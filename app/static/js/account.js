// Your account: two-step sign-in (authenticator app + recovery codes), passkeys, password, signed-in browsers.
import { api, esc, fmtTime, relTime, toast } from "./common.js";
import { createPasskey, getPasskey, passkeyError, passkeysUsable } from "./webauthn.js";

const $ = (id) => document.getElementById(id);
let me = null;

const METHOD = { password: "password", totp: "password + code", recovery: "recovery code", passkey: "passkey",
  sso: "Google", legacy: "before sessions were tracked" };
function browserName(ua) {
  const os = /Windows/.test(ua) ? "Windows" : /Mac OS X|Macintosh/.test(ua) ? "Mac" : /Android/.test(ua) ? "Android"
    : /iPhone|iPad/.test(ua) ? "iPhone/iPad" : /Linux/.test(ua) ? "Linux" : "";
  const br = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "Browser";
  return `${br}${os ? ` on ${os}` : ""}`;
}

function showCodes(codes) {
  $("codes-list").innerHTML = codes.map((c) => `<li>${esc(c)}</li>`).join("");
  $("codes-dialog").showModal();
}
$("codes-done").addEventListener("click", () => $("codes-dialog").close());
$("codes-copy").addEventListener("click", () => navigator.clipboard.writeText(
  [...$("codes-list").querySelectorAll("li")].map((li) => li.textContent).join("\n")).then(() => toast("Copied")));

// ---------------------------------------------------------------- two-step sign-in
function paintMfa() {
  const req = me.mfa_required ? `<div class="alert alert-warn">${esc(me.company)} requires two-step sign-in.</div>` : "";
  if (me.totp_enabled) {
    $("mfa-box").innerHTML = `${req}<p style="margin-top:0"><span class="chip online">On</span> Signing in asks for a code from your authenticator app
        (or a passkey). <b>${me.recovery_left}</b> recovery code${me.recovery_left === 1 ? "" : "s"} left.</p>
      ${me.recovery_left <= 3 ? '<div class="alert alert-warn">You\'re running out of recovery codes. Make new ones.</div>' : ""}
      <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn btn-sm" id="codes-new" type="button">New recovery codes</button>
        <button class="btn btn-sm" id="totp-off" type="button">Turn off</button></div>`;
  } else {
    $("mfa-box").innerHTML = `${req}<p style="margin-top:0"><span class="chip offline">Off</span> Only your password protects this account.
        Add a code from your phone so a stolen password isn't enough.</p>
      <button class="btn btn-primary" id="totp-on" type="button">Set up the authenticator app</button>`;
  }
}
$("mfa-box").addEventListener("click", async (e) => {
  try {
    if (e.target.id === "totp-on") {
      const s = await api("/api/account/totp/setup", { method: "POST" });
      $("totp-qr").src = s.qr; $("totp-secret").textContent = s.secret; $("totp-code").value = ""; $("totp-error").innerHTML = "";
      $("totp-dialog").showModal(); $("totp-code").focus();
    } else if (e.target.id === "codes-new") {
      if (!confirm("Make new recovery codes? The old ones stop working.")) return;
      showCodes((await api("/api/account/recovery-codes", { method: "POST" })).recovery_codes); await load();
    } else if (e.target.id === "totp-off") {
      const code = prompt("To turn off two-step sign-in, type a current code from your authenticator app:");
      if (!code) return;
      await api("/api/account/totp/disable", { method: "POST", body: { code } }); toast("Two-step sign-in is off"); await load();
    }
  } catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
});
$("totp-cancel").addEventListener("click", () => $("totp-dialog").close());
$("totp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/account/totp/enable", { method: "POST", body: { code: $("totp-code").value } });
    $("totp-dialog").close(); showCodes(r.recovery_codes); await load();
  } catch (err) { $("totp-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
});

// ---------------------------------------------------------------- passkeys
function paintPasskeys() {
  if (!me.passkeys_enabled) { $("pk-card").hidden = true; return; }
  const usable = passkeysUsable(me.rp_id);
  const list = me.passkeys.map((k) => `<tr><td><b>${esc(k.name)}</b><div class="muted small-print">Added ${esc(fmtTime(k.created_at))}</div></td>
      <td>${k.last_used_at ? esc(relTime(k.last_used_at)) : '<span class="muted">never used</span>'}</td>
      <td>${k.tested_at ? '<span class="chip online">Works</span>' : '<span class="chip">Not tested</span>'}</td>
      <td style="white-space:nowrap">${usable ? `<button class="btn btn-sm" data-test="${k.id}">Test</button>` : ""}
        <button class="btn btn-sm" data-remove="${k.id}" data-name="${esc(k.name)}">Remove</button></td></tr>`).join("");
  $("pk-box").innerHTML = `${me.passkeys.length ? `<div class="table-wrap"><table><thead><tr><th>Passkey</th><th>Last used</th><th>Status</th><th></th></tr></thead>
      <tbody>${list}</tbody></table></div>` : '<p class="muted">No passkeys yet.</p>'}
    ${usable ? `<form id="pk-add" class="row" style="align-items:flex-end;margin-top:12px">
        <div class="field"><label for="pk-name">Name it</label><input id="pk-name" type="text" maxlength="100" placeholder="e.g. Office PC, iPhone"></div>
        <button class="btn btn-primary" type="submit" style="flex:0 0 auto;margin-bottom:14px">Add a passkey</button></form>`
      : `<p class="muted small-print">Passkeys only work at <b>https://${esc(me.rp_id)}</b>. Open the portal there to add or test one.</p>`}`;
}
$("pk-box").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const options = await api("/api/account/passkeys/options", { method: "POST" });
    const credential = await createPasskey(options);
    const row = await api("/api/account/passkeys", { method: "POST", body: { name: $("pk-name").value, credential } });
    toast("Passkey added. Test it now so you know it works.");
    await load();
    await testKey(row.id);
  } catch (err) { toast(esc(err.status ? err.message : passkeyError(err)), { kind: "error", timeout: 9000 }); }
});
async function testKey(id) {
  const options = await api(`/api/account/passkeys/${id}/test/options`, { method: "POST" });
  const credential = await getPasskey(options);
  await api(`/api/account/passkeys/${id}/test`, { method: "POST", body: { credential } });
  toast("The passkey works"); await load();
}
$("pk-box").addEventListener("click", async (e) => {
  const d = e.target.dataset;
  try {
    if (d.test) await testKey(Number(d.test));
    if (d.remove && confirm(`Remove the passkey "${d.name}"? Also delete it from the device afterwards.`)) {
      await api(`/api/account/passkeys/${d.remove}`, { method: "DELETE" }); toast("Passkey removed"); await load();
    }
  } catch (err) { toast(esc(err.status ? err.message : passkeyError(err)), { kind: "error", timeout: 9000 }); }
});

// ---------------------------------------------------------------- password & sessions
$("pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("pw-error").innerHTML = "";
  if ($("pw-new").value !== $("pw-new2").value) { $("pw-error").innerHTML = '<div class="alert alert-error">The new passwords don\'t match.</div>'; return; }
  try {
    const r = await api("/api/account/password", { method: "POST", body: { current: $("pw-current").value, new: $("pw-new").value } });
    e.target.reset();
    toast(`Password changed${r.other_sessions_ended ? `. ${r.other_sessions_ended} other browser${r.other_sessions_ended === 1 ? " was" : "s were"} signed out` : ""}.`);
    await load();
  } catch (err) { $("pw-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
});

function paintSessions() {
  $("sess-rows").innerHTML = me.sessions.map((s) => `<tr><td><b>${esc(browserName(s.user_agent))}</b>${s.current ? ' <span class="chip online">this one</span>' : ""}
      <div class="muted small-print">Signed in ${esc(fmtTime(s.created_at))} with ${esc(METHOD[s.method] || s.method)}</div></td>
      <td class="mono">${esc(s.ip)}</td><td>${esc(relTime(s.last_seen_at))}</td>
      <td>${s.current ? "" : `<button class="btn btn-sm" data-end="${esc(s.id)}">Sign out</button>`}</td></tr>`).join("")
    || '<tr><td colspan="4" class="empty">None.</td></tr>';
}
$("sess-rows").addEventListener("click", async (e) => {
  if (!e.target.dataset.end) return;
  try { await api(`/api/account/sessions/${encodeURIComponent(e.target.dataset.end)}`, { method: "DELETE" }); toast("Signed out there"); await load(); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});
$("sign-out-all").addEventListener("click", async () => {
  if (!confirm("Sign out every browser, this one included? Monitoring screens signed in as you stop receiving alarms until someone signs in again.")) return;
  await api("/api/account/sign-out-everywhere", { method: "POST" }).catch(() => {});
  location.href = "/login";
});

// ---------------------------------------------------------------- API tokens (MCP server)
async function loadTokens() {
  const r = await api("/api/account/tokens");
  $("tok-url").textContent = r.mcp_url;
  $("tok-rows").innerHTML = r.tokens.map((t) => `<tr><td><b>${esc(t.name)}</b><div class="muted small-print">Created ${esc(fmtTime(t.created_at))}</div></td>
      <td class="mono">${esc(t.hint)}</td>
      <td>${t.last_used_at ? `${esc(relTime(t.last_used_at))}<div class="muted small-print mono">${esc(t.last_used_ip)}</div>` : '<span class="muted">Never</span>'}</td>
      <td>${t.expires_at ? esc(fmtTime(t.expires_at)) : "Never"}</td>
      <td><button class="btn btn-sm" type="button" data-revoke="${t.id}" data-name="${esc(t.name)}">Revoke</button></td></tr>`).join("")
    || '<tr><td colspan="5" class="empty">No tokens.</td></tr>';
}
$("tok-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const days = $("tok-days").value;
    const r = await api("/api/account/tokens", { method: "POST", body: { name: $("tok-name").value, days: days ? Number(days) : null } });
    $("tok-new").innerHTML = `<div class="alert alert-ok"><b>Token "${esc(r.name)}"</b>: copy it now, it won't be shown again.
      <div class="row" style="margin-top:8px;align-items:center"><input type="text" readonly value="${esc(r.token)}" class="mono" id="tok-value" onfocus="this.select()">
      <button class="btn btn-sm" type="button" id="tok-copy" style="flex:0 0 auto">Copy</button></div></div>`;
    $("tok-copy").addEventListener("click", () => {
      $("tok-value").select();
      (navigator.clipboard ? navigator.clipboard.writeText(r.token) : Promise.reject()).then(() => toast("Token copied"))
        .catch(() => { document.execCommand("copy"); toast("Token copied"); });
    });
    e.target.reset(); $("tok-days").value = "90";
    await loadTokens();
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});
$("tok-rows").addEventListener("click", async (e) => {
  const d = e.target.dataset;
  if (!d.revoke || !confirm(`Revoke the token "${d.name}"? Anything using it stops working right away.`)) return;
  try { await api(`/api/account/tokens/${d.revoke}`, { method: "DELETE" }); toast("Token revoked"); $("tok-new").innerHTML = ""; await loadTokens(); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});

async function load() {
  loadTokens().catch((err) => toast(esc(err.message), { kind: "error" }));
  me = await api("/api/account");
  $("acct-who").innerHTML = `${esc(me.display_name || me.email)} · ${esc(me.email)} · ${esc(me.company)} (${me.role === "admin" ? "admin" : "operator"})`;
  $("pw-hint").textContent = me.password_hint;
  paintMfa(); paintPasskeys(); paintSessions();
}
await load().catch((err) => toast(esc(err.message), { kind: "error" }));

// ---------------------------------------------------------------- your own alarm texts
// Same rules as an admin setting it on the Users page: a new number is texted "reply YES" first,
// and alarm texts start only after that reply (carrier rules). STOP stops them.
const fmtPhone = (p) => (/^\+1\d{10}$/.test(p || "") ? `(${p.slice(2, 5)}) ${p.slice(5, 8)}-${p.slice(8)}` : p || "");
const CONSENT = { none: ["Not asked yet", ""], pending: ["Waiting for your YES", "disarmed"], yes: ["On: you replied YES", "online"],
  stopped: ["You replied STOP", "offline"] };
let tx = null;

function paintTexts() {
  const t = tx, some = t.sms_sites != null;
  const [word, cls] = CONSENT[t.sms_consent] || CONSENT.none;
  const status = !t.phone ? "" : `<div class="ed-line"><span class="chip ${cls}">${word}</span><span class="muted small-print">${
    t.sms_consent === "pending" ? `${t.sms_optin_sent_at ? `We texted you ${esc(relTime(t.sms_optin_sent_at))}.` : "We're texting you now."} Reply <b>YES</b> to that text to start getting alarms.`
    : t.sms_consent === "yes" ? `Since ${esc(relTime(t.sms_consent_at))}. Reply STOP to any alarm text to stop them.`
    : t.sms_consent === "stopped" ? "Carriers block texts to you now. Text START or YES to the portal's number to sign up again."
    : "Save your number to get the opt-in text."}</span></div>
    <div class="ed-line">${t.sms_consent === "yes" ? '<button class="btn btn-sm" type="button" id="tx-test">Send me a test text</button>'
      : t.sms_consent === "stopped" || !t.ready ? "" : `<button class="btn btn-sm" type="button" id="tx-optin">${t.sms_consent === "pending" ? "Text me the opt-in again" : "Text me the opt-in"}</button>`}
      ${t.last_text ? `<span class="muted small-print">Last text ${esc(t.last_text.outcome)} ${esc(relTime(t.last_text.ts))}</span>` : ""}</div>`;
  $("tx-box").innerHTML = `${t.ready ? "" : '<div class="alert">Text messages aren\'t switched on for this portal yet. You can save your number now; you\'ll get the opt-in text once they are.</div>'}
    <div class="texts-grid">
      <div class="field"><label for="tx-phone">Mobile number</label><input id="tx-phone" type="tel" maxlength="30" placeholder="(570) 555-1234" value="${esc(fmtPhone(t.phone))}" autocomplete="tel"></div>
      <div class="field"><label for="tx-level">Text me</label><select id="tx-level">
        <option value="alarm">Critical + Alarm</option><option value="critical">Critical only</option><option value="off">No texts</option></select></div>
      <div class="field"><label for="tx-when">When</label><select id="tx-when">
        <option value="raised">When the alarm goes off</option><option value="confirmed">Only when an operator confirms it's real</option>
        <option value="both">Both</option></select></div>
    </div>
    ${t.sites.length > 1 ? `<div class="field"><label>Sites</label>
      <label class="radio inline"><input type="radio" name="tx-scope" value="all" ${some ? "" : "checked"}><span>All sites <small>(and any added later)</small></span></label>
      <label class="radio inline"><input type="radio" name="tx-scope" value="some" ${some ? "checked" : ""}><span>Only these sites</span></label>
      <div class="member-grid" id="tx-sites" ${some ? "" : "hidden"}>${t.sites.map((s) => `<label class="member"><input type="checkbox" value="${s.id}" ${some && t.sms_sites.includes(s.id) ? "checked" : ""}> ${esc(s.name)}</label>`).join("")}</div></div>` : ""}
    ${status}
    <div class="row" style="align-items:center;gap:8px;margin-top:8px"><button class="btn btn-primary" type="submit" style="flex:0 0 auto">Save</button>
      <small class="muted">Adding or changing your number texts you once: "reply YES". <a href="/sms-terms" target="_blank" rel="noopener">Terms</a></small></div>`;
  $("tx-level").value = t.phone ? t.sms_level : "alarm";
  $("tx-when").value = t.sms_when || "raised";
}

async function loadTexts() { tx = await api("/api/account/texts"); paintTexts(); }

$("tx-form").addEventListener("change", (e) => {
  if (e.target.name === "tx-scope") $("tx-sites").hidden = e.target.value !== "some";
});
$("tx-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const scope = document.querySelector('input[name="tx-scope"]:checked')?.value || "all";
  const body = { phone: $("tx-phone").value.trim(), sms_level: $("tx-level").value, sms_when: $("tx-when").value,
    sms_sites: scope === "some" ? [...$("tx-sites").querySelectorAll("input:checked")].map((c) => Number(c.value)) : null };
  const before = tx.sms_consent;
  try {
    tx = await api("/api/account/texts", { method: "PUT", body });
    paintTexts();
    toast(tx.sms_consent === "pending" && before !== "pending" ? "Saved. We've texted you: reply YES to start getting alarm texts." : "Saved");
    if (tx.sms_consent === "pending") setTimeout(() => loadTexts().catch(() => {}), 4000);     // the opt-in text goes in the background
  } catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
});
$("tx-form").addEventListener("click", async (e) => {
  if (e.target.id === "tx-optin") {
    e.target.disabled = true;
    try { tx = await api("/api/account/texts/opt-in", { method: "POST" }); paintTexts(); toast("Sent. Reply YES to that text.");
      setTimeout(() => loadTexts().catch(() => {}), 4000); }
    catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); e.target.disabled = false; }
  } else if (e.target.id === "tx-test") {
    e.target.disabled = true;
    try {
      const r = await api("/api/account/texts/test", { method: "POST" });
      r.outcome === "sent" ? toast("Test text sent") : toast(esc(r.error || r.outcome), { kind: "error", timeout: 12000 });
    } catch (err) { toast(esc(err.message), { kind: "error" }); }
    e.target.disabled = false;
    setTimeout(() => loadTexts().catch(() => {}), 3000);
  }
});
loadTexts().catch((err) => { $("tx-box").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; });

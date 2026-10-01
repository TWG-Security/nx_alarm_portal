// TWG's Platform settings page: sign-in protection (bans, allowlist, locks, attempts), Cloudflare edge bans.
import { api, esc, fmtTime, relTime, toast, can } from "./common.js";

const MANAGE = can("platform.manage");
const $ = (id) => document.getElementById(id);
const errBox = (id, err) => { $(id).innerHTML = err ? `<div class="alert alert-error">${esc(err.message || err)}</div>` : ""; };
let settings = null, security = null;

if (!MANAGE) document.querySelectorAll(".manage-only").forEach((el) => { el.hidden = true; });
document.querySelectorAll(".platform-page input, .platform-page select").forEach((el) => {
  if (!MANAGE && !el.closest("#ev-filter")) el.disabled = true;
});

// ---------------------------------------------------------------- tabs (remembered in the URL hash)
const tabs = $("tabs");
function showTab(name) {
  if (!document.querySelector(`[data-panel="${name}"]`)) name = "signin";
  tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
}
tabs.addEventListener("click", (e) => {
  const t = e.target.closest("button")?.dataset.tab;
  if (t) { history.replaceState(null, "", `#${t}`); showTab(t); }
});
showTab(location.hash.slice(1) || "signin");

// ---------------------------------------------------------------- formatting
const human = (min) => min % 1440 === 0 ? `${min / 1440} d` : min % 60 === 0 ? `${min / 60} h` : `${min} min`;
const OUTCOME = { success: ["Signed in", "online"], failure: ["Failed", "offline"], locked: ["Refused: locked", "offline"],
  denied: ["Refused: company off", "disarmed"], blocked: ["Blocked", "offline"], cleared: ["Cleared", ""] };
const VIA = { cloudflare: "through the Cloudflare Tunnel", direct: "directly (not through Cloudflare)",
  "untrusted-proxy": "through a proxy the portal doesn't trust" };

// ---------------------------------------------------------------- settings
function paintRules() {
  const r = settings.sign_in;
  $("r-max").value = r.max_fails; $("r-window").value = r.window_min; $("r-first").value = r.first_min;
  $("r-maxmin").value = r.max_min; $("r-perm").value = r.permanent_after; $("r-lock").value = r.account_lock_max;
  explainRules();
}
function explainRules() {
  const n = (id) => Number($(id).value) || 0;
  const steps = [];
  for (let i = 1; i <= 6; i++) {
    if (n("r-perm") > 0 && i >= n("r-perm")) { steps.push("permanent"); break; }
    steps.push(human(Math.min(n("r-first") * 4 ** (i - 1), n("r-maxmin"))));
    if (i === 6) steps.push("…");
  }
  $("rules-explain").innerHTML = `<b>${n("r-max")}</b> failed sign-ins from one address within <b>${human(n("r-window"))}</b> block it.
    Each time it comes back for more, the block lasts longer: ${steps.map((s) => `<b>${s}</b>`).join(" → ")}.
    ${n("r-lock") ? `An email with <b>${n("r-lock")}</b> failures in the window is refused, whatever the address, until the window passes.` : "Account locking is off."}`;
}
document.querySelectorAll("#rules-form input").forEach((el) => el.addEventListener("input", explainRules));

$("rules-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  errBox("rules-error");
  const n = (id) => Number($(id).value);
  try {
    settings = await api("/api/platform/settings/sign-in", { method: "PUT", body: { max_fails: n("r-max"), window_min: n("r-window"),
      first_min: n("r-first"), max_min: n("r-maxmin"), permanent_after: n("r-perm"), account_lock_max: n("r-lock") } });
    paintRules(); toast(settings.changed.length ? "Ban rules saved" : "Nothing changed");
  } catch (err) { errBox("rules-error", err); }
});

function paintProxies() {
  $("proxies").value = settings.proxies.trusted;
  $("proxies-env").textContent = settings.proxies.from_env.length
    ? `Also trusted from the server's TRUSTED_PROXY_IPS: ${settings.proxies.from_env.join(", ")}` : "Separate several with commas.";
}
$("proxies-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    settings = await api("/api/platform/settings/proxies", { method: "PUT", body: { trusted: $("proxies").value } });
    paintProxies(); toast("Saved"); await loadSecurity();
  } catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
});

// ---------------------------------------------------------------- cloudflare
function paintCloudflare() {
  const c = settings.cloudflare;
  $("cf-enabled").checked = c.enabled;
  $("cf-zone").value = c.zone_id;
  $("cf-token").value = "";
  $("cf-token").placeholder = c.token_set ? "Saved. Leave blank to keep it." : "Paste the token";
  $("cf-token-hint").textContent = c.token_set ? "Stored encrypted. It's never shown again." : "Stored encrypted; never shown again after saving.";
  $("cf-clear-row").hidden = !c.token_set || !MANAGE;
  $("cf-clear").checked = false;
  const parts = [];
  if (!c.token_set || !c.zone_id) parts.push(`<div class="alert alert-warn">Not set up. Bans only apply inside the portal.</div>`);
  else if (!c.enabled) parts.push(`<div class="alert alert-warn">Switched off. New bans stay inside the portal; existing Cloudflare rules are still cleaned up.</div>`);
  if (c.token_set && !c.token_readable) parts.push(`<div class="alert alert-error">The saved token can't be decrypted (the server's encryption key changed). Paste it again.</div>`);
  if (c.last_error) parts.push(`<div class="alert alert-error"><b>Last error</b> ${esc(relTime(c.last_error_at))}: ${esc(c.last_error)}</div>`);
  if (c.last_ok_at) parts.push(`<div class="alert alert-ok">Last worked ${esc(relTime(c.last_ok_at))} (${esc(fmtTime(c.last_ok_at))}).</div>`);
  $("cf-status").innerHTML = parts.join("");
}
$("cf-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  errBox("cf-error");
  const token = $("cf-token").value.trim();
  const body = { enabled: $("cf-enabled").checked, zone_id: $("cf-zone").value.trim() };
  if ($("cf-clear").checked) body.api_token = "";
  else if (token) body.api_token = token;
  try { settings = await api("/api/platform/settings/cloudflare", { method: "PUT", body }); paintCloudflare(); toast("Cloudflare settings saved"); }
  catch (err) { errBox("cf-error", err); }
});
$("cf-test").addEventListener("click", async () => {
  errBox("cf-error");
  $("cf-test").disabled = true;
  try {
    const r = await api("/api/platform/cloudflare/test", { method: "POST" });
    if (r.ok) toast(`Connected. The zone has ${r.rules} IP Access Rule${r.rules === 1 ? "" : "s"} (${r.ours} made by the portal).`);
    else errBox("cf-error", r.error);
    settings = await api("/api/platform/settings"); paintCloudflare();
  } catch (err) { errBox("cf-error", err); } finally { $("cf-test").disabled = false; }
});

// ---------------------------------------------------------------- two-step sign-in, sessions
function paintTwoFactor() {
  const t = settings.two_factor;
  $("mfa-twg").checked = t.require_twg;
  document.querySelectorAll('input[name="mfa-cust"]').forEach((r) => { r.checked = r.value === t.customers; });
  $("mfa-passkeys").checked = t.passkeys_enabled;
}
$("mfa-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  errBox("mfa-error");
  try {
    settings = await api("/api/platform/settings/two-factor", { method: "PUT", body: { require_twg: $("mfa-twg").checked,
      customers: document.querySelector('input[name="mfa-cust"]:checked')?.value || "company", passkeys_enabled: $("mfa-passkeys").checked } });
    paintTwoFactor(); toast(settings.changed.length ? "Saved. It applies at each person's next sign-in." : "Nothing changed");
  } catch (err) { errBox("mfa-error", err); }
});

function sessWarn() {
  const warn = [];
  if (Number($("s-idle").value) > 0) warn.push(`Screens nobody touches for ${$("s-idle").value} minutes sign out, <b>including wall-mounted monitoring screens</b>. They then stop receiving alarms (with a red banner) until someone signs in.`);
  if (Number($("s-max").value) > 0) warn.push(`Every screen is signed out ${$("s-max").value} hours after signing in, <b>monitoring screens included</b>.`);
  $("sess-warn").innerHTML = warn.map((w) => `<div class="alert alert-warn">${w}</div>`).join("");
}
function paintSessions() {
  const s = settings.sessions;
  $("s-closed").value = s.closed_h; $("s-max").value = s.max_h; $("s-idle").value = s.idle_min;
  sessWarn();
}
["s-max", "s-idle"].forEach((id) => $(id).addEventListener("input", sessWarn));
$("sess-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  errBox("sess-error");
  const body = { closed_h: Number($("s-closed").value), max_h: Number($("s-max").value), idle_min: Number($("s-idle").value) };
  if ((body.max_h || body.idle_min) && !confirm("These settings sign out monitoring screens too. Save anyway?")) return;
  try { settings = await api("/api/platform/settings/sessions", { method: "PUT", body }); paintSessions(); toast("Saved"); }
  catch (err) { errBox("sess-error", err); }
});

// ---------------------------------------------------------------- security overview
function paintSecurity() {
  const s = security;
  $("my-ip").innerHTML = `<p style="margin-top:0">The portal sees you as <b class="mono">${esc(s.my_ip)}</b>, ${esc(VIA[s.my_ip_via] || s.my_ip_via)}.</p>
    <p class="muted">${s.my_ip_protection ? `This address can't be banned: ${esc(s.my_ip_protection)}.` : "This address could be banned like any other."}</p>
    ${MANAGE && !s.my_ip_protection ? `<button class="btn btn-sm" type="button" id="allow-me">Always allow this address</button>` : ""}`;
  $("allow-me")?.addEventListener("click", () => { $("a-ip").value = s.my_ip; $("a-label").focus(); });

  $("untrusted").innerHTML = s.untrusted_connectors.map((u) => `<div class="alert alert-warn">
      <b>${esc(u.peer)}</b> forwarded ${u.count} request${u.count === 1 ? "" : "s"} with a visitor address (last ${esc(relTime(u.last_at))}),
      but isn't trusted, so everyone coming through it looks like ${esc(u.peer)} and can't be told apart (or banned).
      ${MANAGE ? `<button class="btn btn-sm" type="button" data-trust="${esc(u.peer)}">Trust ${esc(u.peer)}</button>` : ""}</div>`).join("");

  $("ban-count").textContent = s.bans.length;
  $("ban-rows").innerHTML = s.bans.map((b) => `<tr>
      <td class="mono"><a href="#" data-ev-ip="${esc(b.ip)}">${esc(b.ip)}</a></td>
      <td>${esc(b.reason)}${b.manual ? ' <span class="chip">by an admin</span>' : ""}</td>
      <td>${b.permanent ? '<span class="chip offline">Permanent</span>' : esc(fmtTime(b.expires_at))}</td>
      <td>${b.at_cloudflare ? '<span class="chip online">Blocked at edge</span>' : s.edge.push ? '<span class="chip">Pending</span>' : '<span class="muted">Portal only</span>'}</td>
      <td>${MANAGE ? `<button class="btn btn-sm" data-unban="${b.id}" data-ip="${esc(b.ip)}">Unblock</button>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="5" class="empty">No addresses are blocked.</td></tr>`;
  if (s.edge_cleanup_pending) $("ban-rows").insertAdjacentHTML("beforeend", `<tr><td colspan="5" class="muted">${s.edge_cleanup_pending} expired
      ban${s.edge_cleanup_pending === 1 ? " is" : "s are"} still waiting to be removed at Cloudflare (retried every minute).</td></tr>`);

  $("allow-rows").innerHTML = s.allowlist.map((a) => `<tr><td class="mono">${esc(a.ip)}</td><td>${esc(a.label)}</td>
      <td>${a.from_env ? '<span class="muted">server setting</span>' : MANAGE ? `<button class="btn btn-sm" data-unallow="${a.id}" data-ip="${esc(a.ip)}">Remove</button>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="3" class="empty">Nothing yet. Add the TWG office's public address.</td></tr>`;

  $("lock-rows").innerHTML = s.locked.map((l) => `<tr><td><a href="#" data-ev-email="${esc(l.email)}">${esc(l.email)}</a></td><td>${l.failures}</td>
      <td>${esc(fmtTime(l.until))}</td><td>${MANAGE ? `<button class="btn btn-sm" data-unlock="${esc(l.email)}">Unlock</button>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="4" class="empty">No locked accounts.</td></tr>`;
}

async function loadSecurity() { security = await api("/api/platform/security"); paintSecurity(); }

async function loadEvents() {
  const q = new URLSearchParams({ ip: $("ev-ip").value.trim(), email: $("ev-email").value.trim(), outcome: $("ev-outcome").value, limit: "200" });
  const rows = await api(`/api/platform/auth-events?${q}`);
  $("ev-rows").innerHTML = rows.map((e) => {
    const [label, cls] = OUTCOME[e.outcome] || [e.outcome, ""];
    return `<tr><td title="${esc(fmtTime(e.ts))}">${esc(relTime(e.ts))}</td><td><span class="chip ${cls}">${esc(label)}</span>${e.kind !== "login" ? ` <span class="muted">${esc(e.kind)}</span>` : ""}</td>
      <td>${e.email ? `<a href="#" data-ev-email="${esc(e.email)}">${esc(e.email)}</a>` : ""}</td>
      <td class="mono">${e.ip ? `<a href="#" data-ev-ip="${esc(e.ip)}">${esc(e.ip)}</a>` : ""}</td><td>${esc(e.company)}</td><td>${esc(e.reason)}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="empty">No sign-in attempts match.</td></tr>`;
}
$("ev-filter").addEventListener("submit", (e) => { e.preventDefault(); loadEvents().catch((err) => toast(esc(err.message), { kind: "error" })); });

// ---------------------------------------------------------------- actions
document.querySelector(".platform-page").addEventListener("click", async (e) => {
  const d = e.target.dataset;
  try {
    if (d.evIp !== undefined || d.evEmail !== undefined) {
      e.preventDefault();
      $("ev-ip").value = d.evIp || ""; $("ev-email").value = d.evEmail || ""; $("ev-outcome").value = "";
      await loadEvents(); $("ev-rows").closest(".card").scrollIntoView({ behavior: "smooth" });
    } else if (d.unban) {
      if (!confirm(`Unblock ${d.ip}? Its earlier failures are forgiven.`)) return;
      await api(`/api/platform/bans/${d.unban}`, { method: "DELETE" }); toast(`${esc(d.ip)} unblocked`); await refresh();
    } else if (d.unallow) {
      if (!confirm(`Remove ${d.ip} from the allowlist? It can be banned again after that.`)) return;
      await api(`/api/platform/allowlist/${d.unallow}`, { method: "DELETE" }); await refresh();
    } else if (d.unlock) {
      await api("/api/platform/locks/clear", { method: "POST", body: { email: d.unlock } }); toast(`${esc(d.unlock)} unlocked`); await refresh();
    } else if (d.trust) {
      const list = [settings.proxies.trusted, d.trust].filter(Boolean).join(", ");
      settings = await api("/api/platform/settings/proxies", { method: "PUT", body: { trusted: list } });
      paintProxies(); toast(`${esc(d.trust)} trusted. Visitors through it now show their real address.`); await refresh();
    }
  } catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
});

$("ban-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/platform/bans", { method: "POST", body: { ip: $("b-ip").value.trim(), minutes: Number($("b-min").value), reason: $("b-reason").value } });
    $("ban-form").reset(); toast("Blocked"); await refresh();
  } catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
});
$("allow-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/platform/allowlist", { method: "POST", body: { ip: $("a-ip").value.trim(), label: $("a-label").value } });
    $("allow-form").reset(); toast("Added to the allowlist"); await refresh();
  } catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
});

async function refresh() { await Promise.all([loadSecurity(), loadEvents()]); }

try {
  settings = await api("/api/platform/settings");
  paintRules(); paintProxies(); paintCloudflare(); paintTwoFactor(); paintSessions();
  await refresh();
} catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 15000);

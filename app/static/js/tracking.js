// Sites page → Guards & vehicles (company admins): the company's own InControl2 credentials, routers with GPS,
// and its tracked guards / vehicles (app/routers/guards.py). The secret is write-only: the page only learns
// whether one is saved.
import { api, esc, relTime, toast } from "./common.js";

const MANAGE = true;            // the section is only on the page for admins
const $ = (id) => document.getElementById(id);
let data = null;

const KIND = { vehicle: "Vehicle", guard: "Guard" };
const CONSENT = { yes: "", pending: " (hasn't replied YES)", none: " (texts not set up)", stopped: " (replied STOP)" };
function personOptions(selected, onlyPhones = false) {
  const people = data.people.filter((p) => !onlyPhones || p.phone);
  return `<option value="">${onlyPhones ? "Pick a person with a phone number" : "Nobody"}</option>` + people.map((p) =>
    `<option value="${p.id}" ${p.id === selected ? "selected" : ""}>${esc(p.name)}${p.phone ? ` · ${esc(p.phone)}${CONSENT[p.consent] ?? ""}` : " (no phone)"}</option>`).join("");
}
function linkState(t) {
  if (t.source !== "phone") return "";
  const live = t.online && t.share_seen_at;
  return `<div class="small-print">${live ? '<span style="color:var(--ok)">Sharing now</span>' : t.share_seen_at ? `Last shared ${esc(relTime(t.share_seen_at))}` : "Link not opened yet"}
    ${t.share_expires_at ? `<span class="muted"> · link valid until ${esc(new Date(t.share_expires_at).toLocaleDateString())}</span>` : ""}</div>
    <div class="row" style="gap:4px;margin-top:4px"><button class="btn btn-sm" type="button" data-share>Text the link</button>
      <button class="btn btn-sm" type="button" data-share-renew title="The old link stops working">New link</button></div>`;
}

function paint() {
  $("gt-enabled").checked = data.enabled;
  $("gt-id").value = data.client_id;
  $("gt-poll").value = data.poll_s;
  $("gt-secret").value = "";
  $("gt-secret").placeholder = data.secret_set ? "Saved (type to replace)" : "";
  $("gt-secret-hint").textContent = data.secret_set ? "Leave blank to keep the saved secret." : "";
  const ok = data.last_ok_at && (!data.last_error_at || data.last_ok_at > data.last_error_at);
  const routers = data.trackers.filter((t) => (t.source || "peplink") === "peplink");
  $("gt-status").innerHTML = !data.enabled ? (routers.length
      ? '<div class="alert alert-error"><b>Router tracking is off</b>, so the routers below get no positions. Tick <b>Track guards</b> and <b>Save</b>.</div>'
      : '<div class="alert">Router tracking (InControl2) is off. Guards tracked by phone work without it.</div>')
    : ok ? `<div class="alert alert-ok">Working: last read from InControl2 ${esc(relTime(data.last_ok_at))}.</div>`
    : data.last_error ? `<div class="alert alert-error">InControl2 problem ${esc(relTime(data.last_error_at))}: ${esc(data.last_error)}</div>`
    : '<div class="alert">On. Waiting for the first read (add a tracker below).</div>';
  $("gt-trackers").innerHTML = data.trackers.map((t) => `<tr data-id="${t.id}">
    <td>${MANAGE ? `<input type="text" value="${esc(t.name)}" maxlength="100" data-f="name" aria-label="Name">` : `<b>${esc(t.name)}</b>`}
      ${MANAGE ? `<select data-f="kind" aria-label="Kind">${Object.entries(KIND).map(([k, v]) => `<option value="${k}" ${k === t.kind ? "selected" : ""}>${v}</option>`).join("")}</select>` : `<div class="muted small-print">${KIND[t.kind]}</div>`}</td>
    <td>${t.source === "phone" ? `<b>Phone (browser)</b>${linkState(t)}` : `${t.source === "fleetio" ? '<span class="chip">Fleetio</span> ' : ""}${esc(t.device_name || "")}<div class="muted small-print mono">${esc(t.source === "fleetio" ? "" : t.serial)}</div>`}
      ${MANAGE ? `<label class="small-print" style="display:block;margin-top:4px">Texts go to <select data-f="user_id" aria-label="Texts go to">${personOptions(t.user_id)}</select></label>` : ""}
      ${t.online === false ? `<div class="small-print guard-offline-text">OFFLINE since ${esc(relTime(t.offline_since))}</div>` : ""}</td>
    <td>${t.fix_at ? `${esc(relTime(t.fix_at))}<div class="muted small-print">${t.lat.toFixed(5)}, ${t.lng.toFixed(5)}</div>` : '<span class="muted">none yet</span>'}
      ${t.error ? `<div class="alert alert-error small-print" style="margin:4px 0 0">${esc(t.error)}</div>` : ""}</td>
    <td class="actions">${MANAGE ? `${t.source === "phone" ? "" : `<button class="btn btn-sm" type="button" data-refresh title="Get its position from ${t.source === "fleetio" ? "Fleetio" : "InControl2"} now">↻ Refresh</button>`}
      <label class="check" style="margin:0"><input type="checkbox" data-f="enabled" ${t.enabled ? "checked" : ""}> On</label>
      <button class="btn btn-sm btn-danger" type="button" data-remove>Remove</button>` : ""}</td></tr>`).join("")
    || '<tr><td colspan="4" class="empty">Nothing tracked yet. Add a router or a phone below.</td></tr>';
  paintFleetio();
  $("gt-auto").checked = data.auto_dispatch;
  const keep = $("gt-person").value;
  $("gt-person").innerHTML = personOptions(Number(keep) || null, true);
}

function shareResult(r) {
  $("gt-phone-result").innerHTML = `<div class="alert ${r.texted ? "alert-ok" : ""}">${r.texted ? "Link texted." : `Not texted: ${esc(r.reason)}. Send them this link another way:`}
    <div class="row" style="gap:6px;margin-top:6px"><input type="text" readonly value="${esc(r.link)}" class="mono" style="flex:1" aria-label="Location link">
    <button class="btn btn-sm" type="button" data-copy="${esc(r.link)}">Copy</button></div></div>`;
}
$("gt-phone-result").addEventListener("click", (e) => {
  if (e.target.dataset.copy) navigator.clipboard?.writeText(e.target.dataset.copy).then(() => toast("Copied"), () => {});
});
$("gt-person").addEventListener("change", () => {
  const p = data.people.find((x) => String(x.id) === $("gt-person").value);
  if (p && !$("gt-person-name").value) $("gt-person-name").value = p.name;
});
$("gt-add-phone").addEventListener("click", async () => {
  const uid = Number($("gt-person").value);
  if (!uid) { toast("Pick the person first", { kind: "error" }); return; }
  try {
    const r = await api("/api/tracking/phones", { method: "POST", body: { user_id: uid, name: $("gt-person-name").value.trim(), kind: $("gt-person-kind").value } });
    data = r; paint(); shareResult(r.share);
    $("gt-person-name").value = "";
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});
$("gt-auto").addEventListener("change", async () => {
  try { data = await api("/api/tracking/dispatch", { method: "PUT", body: { auto: $("gt-auto").checked } }); paint(); toast("Saved"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); paint(); }
});

async function load() {
  data = await api("/api/tracking");
  paint();
}

$("gt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { enabled: $("gt-enabled").checked, client_id: $("gt-id").value.trim(), poll_s: Number($("gt-poll").value) || 30 };
  if ($("gt-secret").value.trim()) body.secret = $("gt-secret").value.trim();
  try {
    data = await api("/api/tracking/source", { method: "PUT", body });
    $("gt-error").innerHTML = "";
    paint();
    toast("Saved");
  } catch (err) { $("gt-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
});

$("gt-test").addEventListener("click", async () => {
  const out = $("gt-test-result");
  out.textContent = "Testing…";
  try {
    const r = await api("/api/tracking/test", { method: "POST" });
    out.innerHTML = r.ok ? `<span style="color:var(--ok)">Connected ✓ ${r.organizations.length} organization${r.organizations.length === 1 ? "" : "s"}: ${esc(r.organizations.slice(0, 6).join(", "))}${r.organizations.length > 6 ? "…" : ""}</span>`
      : `<span class="text-failed">${esc(r.error)}</span>`;
  } catch (err) { out.innerHTML = `<span class="text-failed">${esc(err.message)}</span>`; }
});

// ---- Fleetio: every vehicle in the account on the map
function paintFleetio() {
  const f = data.fleetio;
  $("fl-enabled").checked = f.enabled;
  $("fl-token").value = f.account_token;
  $("fl-poll").value = f.poll_s;
  $("fl-key").value = "";
  $("fl-key").placeholder = f.api_key_set ? "Saved (type to replace)" : "";
  $("fl-key-hint").textContent = f.api_key_set ? "Leave blank to keep the saved key." : "";
  const vans = data.trackers.filter((t) => t.source === "fleetio");
  const ok = f.last_ok_at && (!f.last_error_at || f.last_ok_at > f.last_error_at);
  $("fl-status").innerHTML = !f.enabled ? '<div class="alert">Fleetio is off.</div>'
    : ok ? `<div class="alert alert-ok">Working: ${vans.length} vehicle${vans.length === 1 ? "" : "s"}, last read from Fleetio ${esc(relTime(f.last_ok_at))}.</div>`
    : f.last_error ? `<div class="alert alert-error">Fleetio problem ${esc(relTime(f.last_error_at))}: ${esc(f.last_error)}</div>`
    : '<div class="alert">On. Waiting for the first read.</div>';
}
$("fl-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { enabled: $("fl-enabled").checked, account_token: $("fl-token").value.trim(), poll_s: Number($("fl-poll").value) || 60 };
  if ($("fl-key").value.trim()) body.api_key = $("fl-key").value.trim();
  const btn = e.submitter; if (btn) btn.disabled = true;
  try {
    data = await api("/api/tracking/fleetio", { method: "PUT", body });
    $("fl-error").innerHTML = "";
    paint();
    toast("Saved");
  } catch (err) { $("fl-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
  finally { if (btn) btn.disabled = false; }
});
$("fl-test").addEventListener("click", async () => {
  const out = $("fl-test-result");
  out.textContent = "Testing…";
  try {
    const r = await api("/api/tracking/fleetio/test", { method: "POST" });
    out.innerHTML = r.ok ? `<span style="color:var(--ok)">Connected ✓ ${r.vehicles} vehicle${r.vehicles === 1 ? "" : "s"}, ${r.with_position} with a position${r.newest_position_at ? ` (newest ${esc(relTime(r.newest_position_at))}, oldest ${esc(relTime(r.oldest_position_at))})` : ""}</span>`
      : `<span class="text-failed">${esc(r.error)}</span>`;
  } catch (err) { out.innerHTML = `<span class="text-failed">${esc(err.message)}</span>`; }
});

// ---- add: Organization → Group → Router (only what's picked is asked of InControl2)
let devices = [];
const ago = (iso) => (iso ? relTime(iso) : "");
async function loadOrgs() {
  const sel = $("gt-org");
  sel.innerHTML = '<option value="">Loading…</option>';
  try {
    const orgs = await api("/api/tracking/orgs");
    sel.innerHTML = `<option value="">Pick an organization (${orgs.length})</option>` + orgs.map((o) => `<option value="${esc(o.id)}">${esc(o.name)}</option>`).join("");
  } catch (err) {
    sel.innerHTML = '<option value="">Couldn\'t load organizations</option>';
    $("gt-devices").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
  }
}
$("gt-org").addEventListener("focus", () => { if ($("gt-org").options.length <= 1 && data?.secret_set) loadOrgs(); });
$("gt-org").addEventListener("change", async () => {
  const org = $("gt-org").value, gsel = $("gt-group");
  devices = []; $("gt-devices").innerHTML = ""; $("gt-filter").disabled = true; $("gt-filter").value = "";
  gsel.disabled = true;
  if (!org) { gsel.innerHTML = '<option value="">Pick an organization first</option>'; return; }
  gsel.innerHTML = '<option value="">Loading groups…</option>';
  try {
    const groups = await api(`/api/tracking/orgs/${encodeURIComponent(org)}/groups`);
    gsel.innerHTML = `<option value="">Pick a group (${groups.length})</option>` + groups.map((g) =>
      `<option value="${g.id}">${esc(g.name)} · ${g.online} online${g.offline ? `, ${g.offline} offline` : ""}${g.gps_off ? " · GPS tracking off" : ""}</option>`).join("");
    gsel.disabled = false;
    if (groups.length === 1) { gsel.value = String(groups[0].id); gsel.dispatchEvent(new Event("change")); }
  } catch (err) {
    gsel.innerHTML = '<option value="">No access</option>';
    $("gt-devices").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
  }
});
$("gt-group").addEventListener("change", async () => {
  const org = $("gt-org").value, group = $("gt-group").value, box = $("gt-devices");
  devices = []; box.innerHTML = "";
  if (!group) return;
  box.innerHTML = '<p class="muted">Loading routers…</p>';
  try {
    devices = await api(`/api/tracking/orgs/${encodeURIComponent(org)}/groups/${group}/devices`);
    $("gt-filter").disabled = false;
    paintDevices();
  } catch (err) { box.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
});
$("gt-filter").addEventListener("input", () => paintDevices());

function paintDevices() {
  const q = $("gt-filter").value.trim().toLowerCase();
  const list = devices.map((d, i) => ({ d, i })).filter(({ d }) => !q || `${d.name} ${d.serial} ${d.product}`.toLowerCase().includes(q));
  $("gt-devices").innerHTML = !devices.length ? '<p class="muted">No routers in this group.</p>' : !list.length ? '<p class="muted">No router matches.</p>'
    : `<div class="table-wrap"><table>
      <thead><tr><th>Router</th><th>GPS</th><th>Track as</th></tr></thead><tbody>
      ${list.map(({ d, i }) => `<tr>
        <td><b>${esc(d.name || d.serial)}</b> ${d.online ? '<span class="chip online">online</span>' : '<span class="chip">offline</span>'}
          <div class="muted small-print">${esc(d.product || "")} · <span class="mono">${esc(d.serial || "")}</span></div></td>
        <td class="small-print">${d.gps === "live" ? `Position ${esc(ago(d.fix_at))}` : `<span class="muted">${d.gps_off ? "GPS tracking is off for this group" : "No GPS position (no GPS, or no fix)"}</span>`}</td>
        <td>${d.tracked_as ? `<span class="muted">Tracked as <b>${esc(d.tracked_as)}</b></span>` : d.gps !== "live" ? '<span class="muted">—</span>' : `<div class="row" style="gap:6px;flex-wrap:wrap">
          <input type="text" maxlength="100" value="${esc(d.name || "")}" data-dev-name="${i}" aria-label="Name on the map" style="flex:1 1 140px">
          <select data-dev-kind="${i}" aria-label="Kind"><option value="vehicle">Vehicle</option><option value="guard">Guard</option></select>
          <button class="btn btn-sm btn-primary" type="button" data-track="${i}">Track</button></div>`}</td></tr>`).join("")}
      </tbody></table></div>`;
}

$("gt-devices").addEventListener("click", async (e) => {
  const i = e.target.dataset.track;
  if (i == null) return;
  const d = devices[Number(i)], box = $("gt-devices");
  const name = box.querySelector(`[data-dev-name="${i}"]`).value.trim() || d.name || d.serial;
  try {
    data = await api("/api/tracking/trackers", { method: "POST", body: {
      name, kind: box.querySelector(`[data-dev-kind="${i}"]`).value,
      org: d.org, group: d.group, device: d.device, serial: d.serial || "", device_name: d.name || "" } });
    paint();
    d.tracked_as = name;
    paintDevices();
    toast(`Tracking ${esc(name)}. Its position shows in a few seconds.`);
    for (const ms of [3000, 8000, 15000]) setTimeout(() => load().catch(() => {}), ms);     // the server polls it right away
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});

$("gt-trackers").addEventListener("change", async (e) => {
  const f = e.target.dataset.f, id = e.target.closest("tr")?.dataset.id;
  if (!f || !id) return;
  const v = f === "enabled" ? e.target.checked : f === "user_id" ? (Number(e.target.value) || null) : e.target.value.trim();
  if (f === "name" && !v) { paint(); return; }
  try { data = await api(`/api/tracking/trackers/${id}`, { method: "PUT", body: { [f]: v } }); paint(); toast("Saved"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); paint(); }
});
$("gt-trackers").addEventListener("click", async (e) => {
  if (e.target.matches("[data-share], [data-share-renew]")) {
    const renew = e.target.matches("[data-share-renew]"), id = e.target.closest("tr").dataset.id;
    if (renew && !confirm("Make a new link? The old one stops working.")) return;
    try { const r = await api(`/api/tracking/trackers/${id}/share`, { method: "POST", body: { renew } }); data = r; paint(); shareResult(r.share);
      $("gt-phone-result").scrollIntoView({ block: "nearest" }); }
    catch (err) { toast(esc(err.message), { kind: "error" }); }
    return;
  }
  if (e.target.matches("[data-refresh]")) {
    const id = e.target.closest("tr").dataset.id;
    e.target.disabled = true; e.target.textContent = "Refreshing…";
    try {
      const t = await api(`/api/trackers/${id}/refresh`, { method: "POST" });
      toast(t.error ? `${esc(t.name)}: ${esc(t.error)}` : `${esc(t.name)}: position from ${esc(relTime(t.fix_at))}`, t.error ? { kind: "error" } : {});
    } catch (err) { toast(esc(err.message), { kind: "error" }); }
    await load().catch(() => {});
    return;
  }
  if (!e.target.matches("[data-remove]")) return;
  const tr = e.target.closest("tr"), t = data.trackers.find((x) => String(x.id) === tr.dataset.id);
  if (!confirm(`Stop tracking ${t.name}? Its position history goes too.`)) return;
  try { data = await api(`/api/tracking/trackers/${t.id}`, { method: "DELETE" }); paint(); toast("Removed"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});

load().catch((err) => { $("gt-status").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; });
setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 30000);

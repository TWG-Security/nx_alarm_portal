// Sites page → Guards & vehicles (company admins): the company's own InControl2 credentials, routers with GPS,
// and its tracked guards / vehicles (app/routers/guards.py). The secret is write-only: the page only learns
// whether one is saved.
import { api, esc, relTime, toast } from "./common.js";

const MANAGE = true;            // the section is only on the page for admins
const $ = (id) => document.getElementById(id);
let data = null;

const KIND = { vehicle: "Vehicle", guard: "Guard" };

function paint() {
  $("gt-enabled").checked = data.enabled;
  $("gt-id").value = data.client_id;
  $("gt-poll").value = data.poll_s;
  $("gt-secret").value = "";
  $("gt-secret").placeholder = data.secret_set ? "Saved (type to replace)" : "";
  $("gt-secret-hint").textContent = data.secret_set ? "Leave blank to keep the saved secret." : "";
  const ok = data.last_ok_at && (!data.last_error_at || data.last_ok_at > data.last_error_at);
  $("gt-status").innerHTML = !data.enabled ? '<div class="alert">Guard tracking is off.</div>'
    : ok ? `<div class="alert alert-ok">Working: last read from InControl2 ${esc(relTime(data.last_ok_at))}.</div>`
    : data.last_error ? `<div class="alert alert-error">InControl2 problem ${esc(relTime(data.last_error_at))}: ${esc(data.last_error)}</div>`
    : '<div class="alert">On. Waiting for the first read (add a tracker below).</div>';
  $("gt-trackers").innerHTML = data.trackers.map((t) => `<tr data-id="${t.id}">
    <td>${MANAGE ? `<input type="text" value="${esc(t.name)}" maxlength="100" data-f="name" aria-label="Name">` : `<b>${esc(t.name)}</b>`}
      ${MANAGE ? `<select data-f="kind" aria-label="Kind">${Object.entries(KIND).map(([k, v]) => `<option value="${k}" ${k === t.kind ? "selected" : ""}>${v}</option>`).join("")}</select>` : `<div class="muted small-print">${KIND[t.kind]}</div>`}</td>
    <td>${esc(t.device_name || "")}<div class="muted small-print mono">${esc(t.serial)}</div></td>
    <td>${t.fix_at ? `${esc(relTime(t.fix_at))}<div class="muted small-print">${t.lat.toFixed(5)}, ${t.lng.toFixed(5)}</div>` : '<span class="muted">none yet</span>'}
      ${t.error ? `<div class="alert alert-error small-print" style="margin:4px 0 0">${esc(t.error)}</div>` : ""}</td>
    <td class="actions">${MANAGE ? `<label class="check" style="margin:0"><input type="checkbox" data-f="enabled" ${t.enabled ? "checked" : ""}> On</label>
      <button class="btn btn-sm btn-danger" type="button" data-remove>Remove</button>` : ""}</td></tr>`).join("")
    || '<tr><td colspan="4" class="empty">Nothing tracked yet. Use <b>Find routers</b> below.</td></tr>';
}

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
  const v = f === "enabled" ? e.target.checked : e.target.value.trim();
  if (f === "name" && !v) { paint(); return; }
  try { data = await api(`/api/tracking/trackers/${id}`, { method: "PUT", body: { [f]: v } }); paint(); toast("Saved"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); paint(); }
});
$("gt-trackers").addEventListener("click", async (e) => {
  if (!e.target.matches("[data-remove]")) return;
  const tr = e.target.closest("tr"), t = data.trackers.find((x) => String(x.id) === tr.dataset.id);
  if (!confirm(`Stop tracking ${t.name}? Its position history goes too.`)) return;
  try { data = await api(`/api/tracking/trackers/${t.id}`, { method: "DELETE" }); paint(); toast("Removed"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});

load().catch((err) => { $("gt-status").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; });
setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 30000);

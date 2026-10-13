// Platform page → Guard tracking: InControl2 credentials, routers with GPS, and the tracked guards / vehicles
// (app/routers/guards.py). The secret is write-only: the page only learns whether one is saved.
import { api, esc, relTime, toast, can } from "./common.js";

const MANAGE = can("platform.manage");
const $ = (id) => document.getElementById(id);
let data = null;

const KIND = { vehicle: "Vehicle", guard: "Guard" };
const companyOptions = (sel) => data.companies.map((c) => `<option value="${c.id}" ${c.id === sel ? "selected" : ""}>${esc(c.name)}</option>`).join("");

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
    <td>${MANAGE ? `<select data-f="tenant_id" aria-label="Company">${companyOptions(t.tenant_id)}</select>` : esc(t.company)}</td>
    <td>${esc(t.device_name || "")}<div class="muted small-print mono">${esc(t.serial)}</div></td>
    <td>${t.fix_at ? `${esc(relTime(t.fix_at))}<div class="muted small-print">${t.lat.toFixed(5)}, ${t.lng.toFixed(5)}</div>` : '<span class="muted">none yet</span>'}
      ${t.error ? `<div class="alert alert-error small-print" style="margin:4px 0 0">${esc(t.error)}</div>` : ""}</td>
    <td class="actions">${MANAGE ? `<label class="check" style="margin:0"><input type="checkbox" data-f="enabled" ${t.enabled ? "checked" : ""}> On</label>
      <button class="btn btn-sm btn-danger" type="button" data-remove>Remove</button>` : ""}</td></tr>`).join("")
    || '<tr><td colspan="5" class="empty">Nothing tracked yet. Use <b>Find routers</b> below.</td></tr>';
}

async function load() {
  data = await api("/api/platform/peplink");
  paint();
}

$("gt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { enabled: $("gt-enabled").checked, client_id: $("gt-id").value.trim(), poll_s: Number($("gt-poll").value) || 30 };
  if ($("gt-secret").value.trim()) body.secret = $("gt-secret").value.trim();
  try {
    data = await api("/api/platform/peplink", { method: "PUT", body });
    $("gt-error").innerHTML = "";
    paint();
    toast("Saved");
  } catch (err) { $("gt-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
});

$("gt-test").addEventListener("click", async () => {
  const out = $("gt-test-result");
  out.textContent = "Testing…";
  try {
    const r = await api("/api/platform/peplink/test", { method: "POST" });
    out.innerHTML = r.ok ? `<span style="color:var(--ok)">Connected ✓ ${r.organizations.length} organization${r.organizations.length === 1 ? "" : "s"}: ${esc(r.organizations.slice(0, 6).join(", "))}${r.organizations.length > 6 ? "…" : ""}</span>`
      : `<span class="text-failed">${esc(r.error)}</span>`;
  } catch (err) { out.innerHTML = `<span class="text-failed">${esc(err.message)}</span>`; }
});

let devices = [];
$("gt-find").addEventListener("click", async () => {
  const box = $("gt-devices");
  box.innerHTML = '<p class="muted">Asking InControl2… (a few seconds per organization)</p>';
  try {
    const r = await api("/api/platform/peplink/devices");
    devices = r.devices;
    const own = data.companies.find((c) => c.id === data.trackers[0]?.tenant_id)?.id ?? data.companies[0]?.id;
    box.innerHTML = (devices.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Router</th><th>Organization</th><th>Last position</th><th>Track as</th></tr></thead><tbody>
      ${devices.map((d, i) => `<tr>
        <td><b>${esc(d.name || d.serial)}</b> ${d.online ? '<span class="chip online">online</span>' : '<span class="chip">offline</span>'}
          <div class="muted small-print">${esc(d.product || "")} · <span class="mono">${esc(d.serial || "")}</span></div></td>
        <td>${esc(d.org_name)}</td>
        <td class="small-print">${esc(d.fix_local || "")} <span class="muted">(router's local time)</span><div class="muted">${d.lat.toFixed(5)}, ${d.lng.toFixed(5)}</div></td>
        <td>${d.tracked_as ? `<span class="muted">Tracked as <b>${esc(d.tracked_as)}</b></span>` : `<div class="row" style="gap:6px;flex-wrap:wrap">
          <input type="text" maxlength="100" value="${esc(d.name || "")}" data-dev-name="${i}" aria-label="Name on the map" style="flex:1 1 140px">
          <select data-dev-kind="${i}" aria-label="Kind"><option value="vehicle">Vehicle</option><option value="guard">Guard</option></select>
          <select data-dev-company="${i}" aria-label="Company">${companyOptions(own)}</select>
          <button class="btn btn-sm btn-primary" type="button" data-track="${i}">Track</button></div>`}</td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">No router has reported a GPS position.</p>')
      + (r.problems.length ? `<div class="alert alert-error small-print">${r.problems.map(esc).join("<br>")}</div>` : "");
  } catch (err) { box.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
});

$("gt-devices").addEventListener("click", async (e) => {
  const i = e.target.dataset.track;
  if (i == null) return;
  const d = devices[Number(i)], box = $("gt-devices");
  const name = box.querySelector(`[data-dev-name="${i}"]`).value.trim() || d.name || d.serial;
  try {
    data = await api("/api/platform/trackers", { method: "POST", body: {
      tenant_id: Number(box.querySelector(`[data-dev-company="${i}"]`).value), name, kind: box.querySelector(`[data-dev-kind="${i}"]`).value,
      org: d.org, group: d.group, device: d.device, serial: d.serial || "", device_name: d.name || "" } });
    paint();
    d.tracked_as = name;
    e.target.closest("td").innerHTML = `<span class="muted">Tracked as <b>${esc(name)}</b></span>`;
    toast(`Tracking ${esc(name)}. Its position shows in a few seconds.`);
    for (const ms of [3000, 8000, 15000]) setTimeout(() => load().catch(() => {}), ms);     // the server polls it right away
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});

$("gt-trackers").addEventListener("change", async (e) => {
  const f = e.target.dataset.f, id = e.target.closest("tr")?.dataset.id;
  if (!f || !id) return;
  const v = f === "enabled" ? e.target.checked : f === "tenant_id" ? Number(e.target.value) : e.target.value.trim();
  if (f === "name" && !v) { paint(); return; }
  try { data = await api(`/api/platform/trackers/${id}`, { method: "PUT", body: { [f]: v } }); paint(); toast("Saved"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); paint(); }
});
$("gt-trackers").addEventListener("click", async (e) => {
  if (!e.target.matches("[data-remove]")) return;
  const tr = e.target.closest("tr"), t = data.trackers.find((x) => String(x.id) === tr.dataset.id);
  if (!confirm(`Stop tracking ${t.name}? Its position history goes too.`)) return;
  try { data = await api(`/api/platform/trackers/${t.id}`, { method: "DELETE" }); paint(); toast("Removed"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});

load().catch((err) => { $("gt-status").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; });
setInterval(() => { if (!document.hidden && !$("gt-form").closest("[data-panel]").hidden) load().catch(() => {}); }, 30000);

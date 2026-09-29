import { CFG, api, esc, relTime, on, markerState, STATUS } from "./common.js";

const tbody = document.getElementById("site-rows");
let sites = [];

function render() {
  if (!sites.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No sites yet.${CFG.isAdmin ? ' <a href="/sites/new">Add the first site</a>.' : ""}</td></tr>`;
    return;
  }
  tbody.innerHTML = sites.map((s) => {
    const marker = markerState(s, s.open_security, s.open_system);
    return `<tr>
      <td><div style="display:flex;gap:8px;align-items:center"><span class="dot dot--${marker}"></span><b>${esc(s.name)}</b></div>
        <div class="muted mono">${esc(s.cloud_id || s.host)}</div>${s.address ? `<div class="muted">${esc(s.address)}</div>` : ""}</td>
      <td><span class="chip ${s.status}">${esc(STATUS[s.status] || s.status)}</span>${s.enabled ? "" : ' <span class="chip">disabled</span>'}
        ${s.status_detail ? `<div class="muted" style="max-width:260px">${esc(s.status_detail)}</div>` : ""}</td>
      <td>${s.open_security ? `<a class="badge" href="/alarms?site_id=${s.id}" style="text-decoration:none">${s.open_security}</a>` : '<span class="muted">0</span>'}
        ${s.open_system ? `<span class="chip p3">${s.open_system} system</span>` : ""}</td>
      <td>${esc(s.nx_site_name || "—")}<div class="muted">${s.nx_version ? "v" + esc(s.nx_version) : ""}</div></td>
      <td>${s.camera_count}</td>
      <td>${relTime(s.last_seen_at)}</td>
      ${CFG.isAdmin ? `<td style="white-space:nowrap">
        <a class="btn btn-sm" href="/sites/${s.id}/edit">Edit</a>
        <button class="btn btn-sm" data-op="${s.enabled ? "disable" : "enable"}" data-id="${s.id}">${s.enabled ? "Disable" : "Enable"}</button>
        <button class="btn btn-sm btn-danger" data-op="archive" data-id="${s.id}">Archive</button></td>` : ""}
    </tr>`;
  }).join("");
}

async function load() { sites = await api("/api/sites"); render(); }

tbody.addEventListener("click", async (e) => {
  const op = e.target.dataset.op;
  if (!op) return;
  const s = sites.find((x) => x.id === Number(e.target.dataset.id));
  if (op === "archive" && !confirm(`Archive "${s.name}"? It stops polling and is removed from the map. Its alarm history and audit trail are kept.`)) return;
  e.target.disabled = true;
  try { await api(`/api/sites/${s.id}/${op}`, { method: "POST" }); await load(); }
  catch (err) { alert(err.message); e.target.disabled = false; }
});

for (const ev of ["site.status", "site.updated", "site.removed", "alarm.new", "alarm.acked", "reconnect"]) on(ev, () => load().catch(() => {}));
await load().catch((e) => { tbody.innerHTML = `<tr><td colspan="7" class="empty">${esc(e.message)}</td></tr>`; });

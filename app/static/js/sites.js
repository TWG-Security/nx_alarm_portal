import { CFG, api, esc, relTime, on, openCounts, markerState, STATUS, armChip, armLine, setArmed, ruleHealthHtml, tenantTag } from "./common.js";

const tbody = document.getElementById("site-rows");
let sites = [];

function render() {
  if (!sites.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">No sites yet.${CFG.isAdmin ? ' <a href="/sites/new">Add the first site</a>.' : ""}</td></tr>`;
    return;
  }
  tbody.innerHTML = sites.map((s) => {
    const c = openCounts(s.id);
    const marker = markerState(s, c);
    return `<tr>
      <td><div style="display:flex;gap:8px;align-items:center"><span class="dot dot--${marker}"></span><b>${esc(s.name)}</b>${tenantTag(s)}</div>
        <div class="muted mono">${esc(s.cloud_id || s.host)}</div>${s.address ? `<div class="muted">${esc(s.address)}</div>` : ""}</td>
      <td style="min-width:200px">${armChip(s)} <button class="btn btn-sm" data-arm="${s.arming?.armed === false ? "arm" : "disarm"}" data-id="${s.id}">${s.arming?.armed === false ? "Arm now" : "Disarm…"}</button>
        <div class="arm-line">${armLine(s)}</div></td>
      <td><span class="chip ${s.status}">${esc(STATUS[s.status] || s.status)}</span> ${ruleHealthHtml(s, { brief: true })}${s.enabled ? "" : ' <span class="chip">disabled</span>'}
        ${s.status_detail ? `<div class="muted" style="max-width:260px">${esc(s.status_detail)}</div>` : ""}</td>
      <td>${c[1] + c[2] ? `<a class="badge" href="/alarms?site_id=${s.id}" style="text-decoration:none">${c[1] + c[2]}</a>` : '<span class="muted">0</span>'}
        ${c[3] ? `<span class="chip p3">${c[3]} warning</span>` : ""}</td>
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
  const arm = e.target.dataset.arm;
  if (arm) {
    e.target.disabled = true;
    const s = sites.find((x) => x.id === Number(e.target.dataset.id));
    await setArmed(s, arm === "arm");
    await load().catch(() => {});
    return;
  }
  const op = e.target.dataset.op;
  if (!op) return;
  const s = sites.find((x) => x.id === Number(e.target.dataset.id));
  if (op === "archive" && !confirm(`Archive "${s.name}"? It stops polling and is removed from the map. Its alarm history and audit trail are kept.`)) return;
  e.target.disabled = true;
  try { await api(`/api/sites/${s.id}/${op}`, { method: "POST" }); await load(); }
  catch (err) { alert(err.message); e.target.disabled = false; }
});

for (const ev of ["site.status", "site.updated", "site.removed", "reconnect"]) on(ev, () => load().catch(() => {}));
on("store", render);
await load().catch((e) => { tbody.innerHTML = `<tr><td colspan="8" class="empty">${esc(e.message)}</td></tr>`; });

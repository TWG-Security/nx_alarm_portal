import { CFG, api, esc, fmtTime, relTime, on, openAck, createMap, pinIcon, markerState, STATUS, PRIORITY } from "./common.js";

const sites = new Map();       // id -> site
const openAlarms = new Map();  // id -> alarm (state=new)
const markers = new Map();     // site id -> Leaflet marker
let lmap, selectedId = null;

const DEFAULT_CENTER = [39.8, -98.6];
const SEVERITY = { alarm: 0, warn: 1, offline: 2, ok: 3 };

function counts(siteId) {
  let security = 0, system = 0;
  for (const a of openAlarms.values()) {
    if (a.site_id !== siteId) continue;
    if (a.category === "system") system++; else security++;
  }
  return { security, system };
}

function stateOf(site) {
  const c = counts(site.id);
  return { ...c, marker: markerState(site, c.security, c.system) };
}

// ---------------------------------------------------------------- side panel
function renderStats() {
  let alarm = 0, warn = 0, offline = 0;
  for (const a of openAlarms.values()) (a.category === "system" ? warn++ : alarm++);
  for (const s of sites.values()) if (stateOf(s).marker === "offline") offline++;
  document.getElementById("stat-alarm").textContent = alarm;
  document.getElementById("stat-warn").textContent = warn;
  document.getElementById("stat-offline").textContent = offline;
}

function renderList() {
  const ul = document.getElementById("site-list");
  const list = [...sites.values()].map((s) => ({ s, st: stateOf(s) }))
    .sort((a, b) => SEVERITY[a.st.marker] - SEVERITY[b.st.marker] || a.s.name.localeCompare(b.s.name));
  if (!list.length) {
    ul.innerHTML = `<li class="empty">No sites yet.${CFG.isAdmin ? ' <a href="/sites/new">Add the first site</a>.' : ""}</li>`;
    return;
  }
  ul.innerHTML = list.map(({ s, st }) => `
    <li class="site-item ${s.id === selectedId ? "selected" : ""}" data-id="${s.id}" tabindex="0">
      <span class="dot dot--${st.marker}"></span>
      <span class="name" title="${esc(s.name)}">${esc(s.name)}</span>
      ${st.security ? `<span class="badge">${st.security}</span>` : ""}
      ${st.system ? `<span class="chip p3">${st.system}</span>` : ""}
      ${s.lat == null ? '<span class="chip" title="No location set">no pin</span>' : ""}
    </li>`).join("");
}

function renderDetail() {
  const box = document.getElementById("site-detail");
  const s = sites.get(selectedId);
  if (!s) { box.hidden = true; return; }
  const alarms = [...openAlarms.values()].filter((a) => a.site_id === s.id).sort((a, b) => b.id - a.id);
  box.hidden = false;
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
      <div><h2 style="margin-bottom:2px">${esc(s.name)}</h2>
        <div class="muted">${esc(s.address || "No address")}</div></div>
      <button class="btn btn-sm" id="close-detail" aria-label="Close">✕</button>
    </div>
    <dl class="kv">
      <dt>Status</dt><dd><span class="chip ${s.status}">${esc(STATUS[s.status] || s.status)}</span>${s.enabled ? "" : ' <span class="chip">disabled</span>'}</dd>
      ${s.status_detail ? `<dt>Detail</dt><dd>${esc(s.status_detail)}</dd>` : ""}
      <dt>Last contact</dt><dd>${relTime(s.last_seen_at)}</dd>
      <dt>NX</dt><dd>${esc(s.nx_site_name || "—")} ${s.nx_version ? `<span class="muted">v${esc(s.nx_version)}</span>` : ""}</dd>
      <dt>Cameras</dt><dd>${s.camera_count}</dd>
      ${s.notes ? `<dt>Notes</dt><dd style="white-space:pre-wrap">${esc(s.notes)}</dd>` : ""}
    </dl>
    <h3>Open alarms (${alarms.length})</h3>
    ${alarms.length ? `<ul class="alarm-list">${alarms.slice(0, 20).map((a) => `
      <li class="alarm p${a.priority}" data-alarm="${a.id}">
        <span class="stripe"></span>
        <div><div class="title">${esc(a.caption)}</div>
          <div class="meta"><span>${esc(a.source_name || a.event_type)}</span><span>${fmtTime(a.event_ts_ms)}</span></div></div>
        <div class="right"><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span>
          <button class="btn btn-sm btn-primary" data-ack="${a.id}">Ack</button></div>
      </li>`).join("")}</ul>` : '<p class="muted">No open alarms.</p>'}
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <a class="btn btn-sm" href="/alarms?site_id=${s.id}">All alarms for site</a>
      ${CFG.isAdmin ? `<a class="btn btn-sm" href="/sites/${s.id}/edit">Edit site</a>` : ""}
    </div>`;
}

function renderAll() {
  renderStats(); renderList(); renderDetail();
  for (const s of sites.values()) upsertMarker(s);
}

function select(id, pan = true) {
  selectedId = id;
  renderList(); renderDetail();
  for (const s of sites.values()) upsertMarker(s);
  const s = sites.get(id);
  if (pan && lmap && s?.lat != null) lmap.setView([s.lat, s.lng], Math.max(lmap.getZoom(), 14));
}

document.getElementById("site-list").addEventListener("click", (e) => {
  const li = e.target.closest(".site-item");
  if (li) select(Number(li.dataset.id));
});
document.getElementById("site-list").addEventListener("keydown", (e) => {
  const li = e.target.closest(".site-item");
  if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); select(Number(li.dataset.id)); }
});
document.getElementById("site-detail").addEventListener("click", async (e) => {
  if (e.target.id === "close-detail") { selectedId = null; renderAll(); return; }
  const ackId = e.target.dataset.ack;
  if (ackId) {
    e.stopPropagation();
    const updated = await openAck(openAlarms.get(Number(ackId)));
    if (updated) { openAlarms.delete(updated.id); renderAll(); }
    return;
  }
  const row = e.target.closest("[data-alarm]");
  if (row) location.href = `/alarms?open=${row.dataset.alarm}`;
});

// ---------------------------------------------------------------- map
function upsertMarker(site) {
  if (!lmap) return;
  const existing = markers.get(site.id);
  if (site.lat == null || site.lng == null) {
    if (existing) { existing.remove(); markers.delete(site.id); }
    return;
  }
  const st = stateOf(site);
  const icon = pinIcon(st.marker, st.security + st.system, site.name, site.id === selectedId);
  const title = `${site.name}: ${st.marker === "alarm" ? `${st.security} open alarm(s)` : STATUS[site.status] || site.status}`;
  const z = (3 - SEVERITY[st.marker]) * 1000;
  if (existing) {
    existing.setLatLng([site.lat, site.lng]).setIcon(icon).setZIndexOffset(z);
    existing.getElement()?.setAttribute("title", title);
    return;
  }
  const m = L.marker([site.lat, site.lng], { icon, title, zIndexOffset: z, keyboard: true, alt: site.name }).addTo(lmap);
  m.on("click", () => select(site.id, false));
  markers.set(site.id, m);
}

function fitToSites() {
  const pts = [...sites.values()].filter((s) => s.lat != null).map((s) => [s.lat, s.lng]);
  if (!pts.length) return lmap.setView(DEFAULT_CENTER, 4);
  if (pts.length === 1) return lmap.setView(pts[0], 13);
  lmap.fitBounds(pts, { padding: [60, 60], maxZoom: 15 });
}

// ---------------------------------------------------------------- data + live updates
async function loadData() {
  const [siteRows, alarmRows] = await Promise.all([api("/api/sites"), api("/api/alarms?state=open&limit=500")]);
  sites.clear(); openAlarms.clear();
  siteRows.forEach((s) => sites.set(s.id, s));
  alarmRows.forEach((a) => openAlarms.set(a.id, a));
  renderAll();
}

on("alarm.new", (a) => { openAlarms.set(a.id, a); renderAll(); });
on("alarm.acked", (a) => { openAlarms.delete(a.id); renderAll(); });
on("site.status", ({ site_id, status, detail }) => {
  const s = sites.get(site_id);
  if (s) { s.status = status; s.status_detail = detail || ""; if (status === "online") s.last_seen_at = new Date().toISOString(); renderAll(); }
});
on("site.updated", (s) => { sites.set(s.id, s); renderAll(); });
on("site.removed", (s) => { sites.delete(s.id); markers.get(s.id)?.remove(); markers.delete(s.id); renderAll(); });
on("reconnect", () => loadData().catch(() => {}));
setInterval(() => { if (selectedId) renderDetail(); }, 30000); // keep "last contact" fresh

lmap = createMap(document.getElementById("map"), { center: DEFAULT_CENTER, zoom: 4 });
await loadData().catch((e) => { document.getElementById("site-list").innerHTML = `<li class="empty">${esc(e.message)}</li>`; });
fitToSites();

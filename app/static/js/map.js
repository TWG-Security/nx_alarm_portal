// Overview: sites (left), map (center), live alarm feed with active timers (right).
import { CFG, api, esc, fmtTime, relTime, timerHtml, on, openAlarms, openCounts, openAck, createMap, pinIcon,
         markerState, STATUS, PRIORITY, armChip, armLine, setArmed, ruleHealthHtml } from "./common.js";
import { openDrawer } from "./drawer.js";

const sites = new Map();     // id -> site
const markers = new Map();   // site id -> Leaflet marker
let selectedId = null, feedPriority = "";
const SEVERITY = { alarm: 0, warn: 1, offline: 2, ok: 3 };
const DEFAULT_CENTER = [39.8, -98.6];
const lmap = createMap(document.getElementById("map"), { center: DEFAULT_CENTER, zoom: 4 });

const stateOf = (site) => { const c = openCounts(site.id); return { c, marker: markerState(site, c) }; };

// ---------------------------------------------------------------- left: stats, sites, site detail
function renderStats() {
  const c = openCounts();
  let offline = 0;
  for (const s of sites.values()) if (stateOf(s).marker === "offline") offline++;
  document.getElementById("stat-critical").textContent = c[1];
  document.getElementById("stat-alarm").textContent = c[2];
  document.getElementById("stat-warn").textContent = c[3];
  document.getElementById("stat-offline").textContent = offline;
}

function renderSites() {
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
      ${s.arming?.armed === false ? '<span class="chip disarmed" title="Security events are recorded but not raised">Disarmed</span>' : ""}
      ${st.c[1] + st.c[2] ? `<span class="badge">${st.c[1] + st.c[2]}</span>` : ""}
      ${st.c[3] ? `<span class="chip p3">${st.c[3]}</span>` : ""}
      ${s.lat == null ? '<span class="chip" title="No location set">no pin</span>' : ""}
    </li>`).join("");
}

function renderDetail() {
  const box = document.getElementById("site-detail");
  const s = sites.get(selectedId);
  if (!s) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
      <div><h2 style="margin-bottom:2px">${esc(s.name)}</h2><div class="muted">${esc(s.address || "No address")}</div></div>
      <button class="btn btn-sm" id="close-detail" aria-label="Close">✕</button>
    </div>
    <dl class="kv">
      <dt>Arming</dt><dd>${armChip(s)}
        <button class="btn btn-sm" data-arm="${s.arming?.armed === false ? "arm" : "disarm"}" style="margin-left:6px">${s.arming?.armed === false ? "Arm now" : "Disarm…"}</button>
        <div class="arm-line">${armLine(s)}</div></dd>
      <dt>Status</dt><dd><span class="chip ${s.status}">${esc(STATUS[s.status] || s.status)}</span>${s.enabled ? "" : ' <span class="chip">disabled</span>'}</dd>
      ${s.status_detail ? `<dt>Detail</dt><dd>${esc(s.status_detail)}</dd>` : ""}
      <dt>Last contact</dt><dd>${relTime(s.last_seen_at)}</dd>
      <dt>Alarm feed</dt><dd>${s.push ? '<span class="chip online">Live push</span>' : s.push === false ? '<span class="chip">Polling only (5 s)</span>' : "—"}</dd>
      <dt>NX rules</dt><dd>${ruleHealthHtml(s)}</dd>
      <dt>NX</dt><dd>${esc(s.nx_site_name || "—")} ${s.nx_version ? `<span class="muted">v${esc(s.nx_version)}</span>` : ""}</dd>
      <dt>Cameras</dt><dd>${s.camera_count}</dd>
      ${s.notes ? `<dt>Notes</dt><dd style="white-space:pre-wrap">${esc(s.notes)}</dd>` : ""}
    </dl>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <a class="btn btn-sm" href="/alarms?site_id=${s.id}&state=all">Alarm history</a>
      ${CFG.isAdmin ? `<a class="btn btn-sm" href="/sites/${s.id}/edit">Edit site</a>` : ""}
    </div>`;
}

// ---------------------------------------------------------------- right: live feed
function feedRows() {
  return [...openAlarms.values()]
    .filter((a) => (!selectedId || a.site_id === selectedId))
    .sort((a, b) => a.priority - b.priority || b.event_ts_ms - a.event_ts_ms);
}

function renderFeed() {
  const all = feedRows();
  const counts = { "": all.length, 1: 0, 2: 0, 3: 0 };
  all.forEach((a) => counts[a.priority]++);
  document.querySelectorAll("#feed-tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.p === feedPriority);
    b.querySelector("span").textContent = counts[b.dataset.p] || "";
  });
  const filt = document.getElementById("feed-site-filter");
  const site = sites.get(selectedId);
  filt.hidden = !site;
  if (site) filt.innerHTML = `Showing <b>${esc(site.name)}</b> only <button class="btn btn-sm" id="clear-site">Show all sites</button>`;

  const rows = feedPriority ? all.filter((a) => String(a.priority) === feedPriority) : all;
  const ul = document.getElementById("feed-list");
  if (!rows.length) {
    ul.innerHTML = `<li class="empty feed-clear"><span class="dot dot--ok"></span> ${all.length ? "Nothing at this level." : "All clear. No open alarms."}</li>`;
    return;
  }
  ul.innerHTML = rows.map((a) => `
    <li class="feed-item p${a.priority}" data-id="${a.id}" tabindex="0">
      <div class="feed-top"><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span>${timerHtml(a.event_ts_ms)}</div>
      <div class="title">${esc(a.caption)}</div>
      <div class="feed-where"><b>${esc(a.site_name)}</b>${a.site_address ? ` · ${esc(a.site_address)}` : ""}</div>
      <div class="meta">${a.source_name ? `${esc(a.source_name)} · ` : ""}${fmtTime(a.event_ts_ms)}</div>
      <div class="feed-actions"><button class="btn btn-sm btn-primary" data-ack="${a.id}">Acknowledge</button>
        <button class="btn btn-sm" data-detail="${a.id}">Details</button></div>
    </li>`).join("");
}

// ---------------------------------------------------------------- map
function upsertMarker(site) {
  const existing = markers.get(site.id);
  if (site.lat == null || site.lng == null) {
    if (existing) { existing.remove(); markers.delete(site.id); }
    return;
  }
  const { c, marker } = stateOf(site);
  const icon = pinIcon(marker, c[1] + c[2] + c[3], site.name, site.id === selectedId, site.arming?.armed === false);
  const title = `${site.name}: ${marker === "offline" ? STATUS[site.status] || "Offline" : `${c[1] + c[2]} alarm(s), ${c[3]} warning(s)`}`;
  const z = (3 - SEVERITY[marker]) * 1000;
  if (existing) {
    existing.setLatLng([site.lat, site.lng]).setIcon(icon).setZIndexOffset(z);
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

function renderAll() {
  renderStats(); renderSites(); renderDetail(); renderFeed();
  for (const s of sites.values()) upsertMarker(s);
}

function select(id, pan = true) {
  selectedId = id;
  renderAll();
  const s = sites.get(id);
  if (pan && s?.lat != null) lmap.flyTo([s.lat, s.lng], Math.max(lmap.getZoom(), 15), { duration: 0.8 });
}

function focusAlarm(a) {
  if (sites.has(a.site_id)) select(a.site_id);
  openDrawer(a.id, a);
}

// ---------------------------------------------------------------- interactions
const siteList = document.getElementById("site-list");
siteList.addEventListener("click", (e) => { const li = e.target.closest(".site-item"); if (li) select(Number(li.dataset.id)); });
siteList.addEventListener("keydown", (e) => {
  const li = e.target.closest(".site-item");
  if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); select(Number(li.dataset.id)); }
});
document.getElementById("site-detail").addEventListener("click", async (e) => {
  if (e.target.id === "close-detail") { selectedId = null; renderAll(); return; }
  const op = e.target.dataset.arm;
  if (op && sites.has(selectedId)) { e.target.disabled = true; await setArmed(sites.get(selectedId), op === "arm"); e.target.disabled = false; }
});
document.getElementById("feed-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  feedPriority = b.dataset.p; renderFeed();
});
document.getElementById("feed-site-filter").addEventListener("click", (e) => { if (e.target.id === "clear-site") { selectedId = null; renderAll(); } });
const feed = document.getElementById("feed-list");
feed.addEventListener("click", async (e) => {
  const ack = e.target.dataset.ack;
  if (ack) { e.stopPropagation(); await openAck(openAlarms.get(Number(ack))); return; }
  const li = e.target.closest(".feed-item");
  if (li) focusAlarm(openAlarms.get(Number(li.dataset.id)));
});
feed.addEventListener("keydown", (e) => {
  const li = e.target.closest(".feed-item");
  if (li && e.key === "Enter") focusAlarm(openAlarms.get(Number(li.dataset.id)));
});

// ---------------------------------------------------------------- data + live updates
async function loadSites() {
  const rows = await api("/api/sites");
  sites.clear();
  rows.forEach((s) => sites.set(s.id, s));
}

on("store", renderAll);
on("alarm.arrived", (a) => {
  // Draw the eye: flash the new card and the site's pin.
  requestAnimationFrame(() => document.querySelector(`.feed-item[data-id="${a.id}"]`)?.classList.add("fresh"));
});
on("site.status", ({ site_id, status, detail }) => {
  const s = sites.get(site_id);
  if (s) { s.status = status; s.status_detail = detail || ""; if (status === "online") s.last_seen_at = new Date().toISOString(); renderAll(); }
});
on("site.updated", (s) => { sites.set(s.id, { ...sites.get(s.id), ...s }); renderAll(); });
on("site.push", ({ site_id, push }) => { const s = sites.get(site_id); if (s) { s.push = push; renderDetail(); } });
on("site.removed", (s) => { sites.delete(s.id); markers.get(s.id)?.remove(); markers.delete(s.id); renderAll(); });
on("reconnect", () => loadSites().then(renderAll).catch(() => {}));
on("focus-alarm", focusAlarm);
setInterval(() => { if (selectedId) renderDetail(); }, 30000);

await loadSites().catch((e) => { siteList.innerHTML = `<li class="empty">${esc(e.message)}</li>`; });
renderAll();
fitToSites();
const focusId = Number(new URLSearchParams(location.search).get("alarm"));
if (focusId) {
  history.replaceState(null, "", "/");
  const a = openAlarms.get(focusId);
  if (a) focusAlarm(a); else api(`/api/alarms/${focusId}`).then(focusAlarm).catch(() => {});
}

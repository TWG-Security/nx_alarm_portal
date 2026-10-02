// Overview: sites (left), map (center), live alarm feed with active timers (right).
import { CFG, api, esc, toast, fmtTime, relTime, timerHtml, on, openAlarms, openCounts, openAck, createMap, pinIcon, markerState, STATUS, PRIORITY, setArmed, armCardHtml, ruleHealthHtml, inView, tenantTag, multiCompany } from "./common.js";
import { openDrawer } from "./drawer.js";

const sites = new Map();     // id -> site
const markers = new Map();   // site id -> Leaflet marker
let selectedId = null, feedPriority = "";
let globe = null, globeOpening = false;   // the 3D view (globe.js), loaded on first use
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
      <span class="name" title="${esc(s.tenant_name ? `${s.tenant_name}: ${s.name}` : s.name)}">${esc(s.name)}</span>${tenantTag(s)}
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
      <div><h2 style="margin-bottom:2px">${esc(s.name)}</h2>${multiCompany ? `<div><b>${esc(s.tenant_name)}</b></div>` : ""}<div class="muted">${esc(s.address || "No address")}</div></div>
      <button class="btn btn-sm" id="close-detail" aria-label="Close">✕</button>
    </div>
    ${armCardHtml(s)}
    <dl class="kv">
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
      ${s.lat != null ? '<button class="btn btn-sm btn-primary" type="button" data-globe title="Fly into a 3D aerial view of this site">3D view</button>' : ""}
      <a class="btn btn-sm" href="/alarms?site_id=${s.id}&state=all">Alarm history</a>
      ${CFG.isAdmin ? `<a class="btn btn-sm" href="/sites/${s.id}/edit">Edit site</a>` : ""}
    </div>`;
}

// ---------------------------------------------------------------- right: live feed
function feedRows() {
  return [...openAlarms.values()]
    .filter((a) => inView(a) && (!selectedId || a.site_id === selectedId))
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
      <div class="feed-where">${tenantTag(a)}<b>${esc(a.site_name)}</b>${a.site_address ? ` · ${esc(a.site_address)}` : ""}</div>
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
  globe?.update(); renderHud();
}

function select(id, pan = true) {
  selectedId = id;
  renderAll();
  const s = sites.get(id);
  if (pan && s?.lat != null) lmap.flyTo([s.lat, s.lng], Math.max(lmap.getZoom(), 15), { duration: 0.8 });
  if (pan && globe) globe.flyTo(s);
}

// ---------------------------------------------------------------- 3D view (over the map pane)
const $id = (x) => document.getElementById(x);
const globeEl = $id("globe"), globeBar = $id("globe-bar"), globeMsg = $id("globe-msg"), hud = $id("hud");
const camPanel = $id("cam-panel"), camPlace = $id("cam-place"), camsBtn = $id("globe-cams");
let hudClock = null, camLive = null, camOpen = null;   // camOpen = { siteId, deviceId }

const globePins = () => [...sites.values()].map((s) => {
  const { c, marker } = stateOf(s);
  return { id: s.id, name: s.name, lat: s.lat, lng: s.lng, state: marker, count: c[1] + c[2] + c[3], disarmed: s.arming?.armed === false };
});
const camAlarm = (siteId, deviceId) => [...openAlarms.values()].find((a) => a.site_id === siteId && a.device_id === deviceId && inView(a));
const globeCams = () => [...sites.values()].flatMap((s) => Object.entries(s.camera_places || {}).map(([deviceId, p]) =>
  ({ siteId: s.id, deviceId, ...p, alarm: !!camAlarm(s.id, deviceId) })));

// HUD text: the selected site, a UTC clock, counts, and where the view is centred.
const STATE_TXT = { ok: "NORMAL", warn: "WARNING", alarm: "ALARM", offline: "OFFLINE" };
function renderHud() {
  if (!globe) return;
  const s = sites.get(selectedId);
  if (s) {
    const { c, marker } = stateOf(s);
    const n = c[1] + c[2] + c[3];
    $id("hud-site").innerHTML = `${esc(s.name.toUpperCase())}<br><span class="hud-sub">${esc((s.address || "").toUpperCase())}</span><br>
      <span class="st ${marker}">${STATE_TXT[marker]}${n ? ` · ${n} OPEN` : ""}</span>${s.arming?.armed === false ? ' <span class="st warn">DISARMED</span>' : ""}`;
  } else $id("hud-site").textContent = "";
  const open = [...openAlarms.values()].filter(inView).length;
  const placed = [...sites.values()].reduce((n, x) => n + Object.keys(x.camera_places || {}).length, 0);
  $id("hud-counts").textContent = `SITES ${sites.size} · CAMERAS ${placed} · OPEN ALARMS ${open}`;
}
const dms = (v, pos, neg) => {
  const a = Math.abs(v), d = Math.floor(a), m = Math.floor((a - d) * 60), sec = ((a - d) * 60 - m) * 60;
  return `${d}°${String(m).padStart(2, "0")}'${sec.toFixed(1).padStart(4, "0")}"${v >= 0 ? pos : neg}`;
};
function renderCoords({ lat, lng, alt, heading }) {
  $id("hud-coords").innerHTML = `${dms(lat, "N", "S")}  ${dms(lng, "E", "W")}<br>
    ${lat.toFixed(5)}, ${lng.toFixed(5)}<br>ALT ${Math.round(alt).toLocaleString()} M · HDG ${String(Math.round(heading)).padStart(3, "0")}°`;
}
const tickClock = () => { $id("hud-clock").textContent = new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC"; };

async function openGlobeView(site) {
  if (globe) { globe.flyTo(site); return; }
  if (globeOpening) return;
  globeOpening = true;
  globeEl.hidden = false; globeBar.hidden = false;
  globeMsg.textContent = "Loading 3D view…"; globeMsg.hidden = false;
  try {
    const { openGlobe } = await import("./globe.js");
    globe = await openGlobe(globeEl, {
      pins: globePins, cameras: globeCams,
      onPick: (id) => select(id),
      onPickCamera: openCamPanel,
      onView: renderCoords,
      onTooSlow: (why) => {
        closeGlobeView();
        toast(`3D view closed to keep alarms fast: ${esc(why)}. The 2D map is back.`, { kind: "warn", timeout: 10000 });
      },
    });
    window.__globeStats = globe.stats;
    window.__globe = globe.viewer;   // for tools/e2e/globe.py and debugging
    globeMsg.hidden = true;
    hud.hidden = false; camsBtn.hidden = !CFG.isAdmin;
    $id("imagery-3d").textContent = globe.hasBuildings ? "3D" : "Satellite";
    tickClock(); hudClock = setInterval(tickClock, 1000);
    renderHud();
    globe.flyTo(sites.get(selectedId) || site, { fromSpace: true });
  } catch (err) {
    console.error("3D view failed", err);
    closeGlobeView();
    toast(`3D view isn't available on this screen (${esc(err.message || String(err))}). The 2D map and alarms are unaffected.`, { kind: "error", timeout: 8000 });
  } finally {
    globeOpening = false;
  }
}

function closeGlobeView() {
  closeCamPanel(); closePlacing();
  try { globe?.destroy(); } catch (err) { console.error(err); }
  globe = null; delete window.__globe; delete window.__globeStats;
  clearInterval(hudClock); hudClock = null;
  globeEl.hidden = true; globeBar.hidden = true; globeMsg.hidden = true; hud.hidden = true;
  globeEl.replaceChildren();
}

// ---- live camera window: click a camera on the 3D view
async function openCamPanel(siteId, deviceId) {
  const s = sites.get(siteId), p = s?.camera_places?.[deviceId];
  if (!p) return;
  closeCamPanel();
  camOpen = { siteId, deviceId };
  const a = camAlarm(siteId, deviceId);
  camPanel.classList.toggle("alarm", !!a);
  camPanel.innerHTML = `<div class="cam-head"><span><b>${esc(p.name || "Camera")}</b> <span class="muted">· ${esc(s.name)}</span></span>
      <span>${a ? `<button type="button" data-cam-alarm="${a.id}" title="Open this alarm">ALARM ▸</button>` : ""}<button type="button" data-cam-close aria-label="Close">✕</button></span></div>
    <div class="cam-live"></div>`;
  camPanel.hidden = false;
  const { mountLive } = await import("./live.js");
  if (camOpen?.deviceId !== deviceId) return;
  camLive = mountLive(camPanel.querySelector(".cam-live"), { id: 0, device_id: deviceId },
    { label: false, base: `/media/sites/${siteId}/cameras/${encodeURIComponent(deviceId)}` });
}
function closeCamPanel() {
  try { camLive?.destroy(); } catch (err) { console.error(err); }
  camLive = null; camOpen = null; camPanel.hidden = true; camPanel.replaceChildren();
}
camPanel.addEventListener("click", (e) => {
  if (e.target.closest("[data-cam-close]")) return closeCamPanel();
  const id = e.target.closest("[data-cam-alarm]")?.dataset.camAlarm;
  if (id) { const a = openAlarms.get(Number(id)); if (a) focusAlarm(a); }
});

// ---- place cameras (admins): pick a camera, click its spot, then the direction it faces
const FOVS = [[60, "Narrow 60°"], [90, "Wide 90°"], [120, "Ultra-wide 120°"], [360, "360° dome"]];
let placingSite = null;
async function openPlacing() {
  const s = sites.get(selectedId);
  if (!s) { toast("Select a site first.", { kind: "warn" }); return; }
  placingSite = s.id;
  camsBtn.classList.add("active");
  camPlace.hidden = false;
  camPlace.innerHTML = `<div class="hint">Loading ${esc(s.name)}'s cameras…</div>`;
  try {
    const cams = await api(`/api/sites/${s.id}/cameras`);
    if (placingSite !== s.id) return;
    camPlace.innerHTML = `<div class="hint" id="place-hint">Pick a camera, then click where it is on the map.</div>
      <ul>${cams.map((c) => `<li data-dev="${esc(c.device_id)}">
        <span class="nm" title="${esc(c.name)}">${esc(c.name)}${c.place ? ' <span class="placed">✓ placed</span>' : ""}</span>
        <select aria-label="View width">${FOVS.map(([v, t]) => `<option value="${v}"${(c.place?.fov || 60) === v ? " selected" : ""}>${t}</option>`).join("")}</select>
        <button type="button" class="go" data-place>${c.place ? "Move" : "Place"}</button>
        ${c.place ? '<button type="button" data-unplace title="Take it off the 3D view">✕</button>' : ""}</li>`).join("") || '<li class="nm">No cameras found on this site.</li>'}</ul>`;
  } catch (err) {
    camPlace.innerHTML = `<div class="hint">Couldn't list cameras: ${esc(err.message)}</div>`;
  }
}
function closePlacing() {
  globe?.cancelPlacing();
  placingSite = null; camsBtn.classList.remove("active");
  camPlace.hidden = true; camPlace.replaceChildren();
}
async function saveSite(data) { sites.set(data.id, { ...sites.get(data.id), ...data }); renderAll(); }
camPlace.addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-dev]");
  if (!li || !globe || placingSite == null) return;
  const dev = li.dataset.dev, name = li.querySelector(".nm").textContent.replace("✓ placed", "").trim();
  const site = sites.get(placingSite);
  if (e.target.closest("[data-unplace]")) {
    await saveSite(await api(`/api/sites/${site.id}/cameras/${encodeURIComponent(dev)}`, { method: "DELETE" }));
    return openPlacing();
  }
  if (!e.target.closest("[data-place]")) return;
  const fov = Number(li.querySelector("select").value);
  const hint = $id("place-hint");
  hint.textContent = `Click where ${name} is.  (Esc to cancel)`;
  const done = globe.placeCamera({ fov });
  globe.onPlacingStep(() => { hint.textContent = `Now click the direction ${name} faces.`; });
  const spot = await done;
  if (!spot) { if ($id("place-hint")) $id("place-hint").textContent = "Pick a camera, then click where it is on the map."; return; }
  try {
    await saveSite(await api(`/api/sites/${site.id}/cameras/${encodeURIComponent(dev)}`, { method: "PUT", body: spot }));
    toast(`${esc(name)} placed.`, { kind: "ok", timeout: 3000 });
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
  openPlacing();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && globe?.placingStep) globe.cancelPlacing(); });

globeBar.addEventListener("click", (e) => {
  const t = e.target;
  if (t.id === "globe-close") return closeGlobeView();
  if (t.id === "globe-recenter") return globe?.flyTo(sites.get(selectedId));
  if (t.id === "globe-cams") return placingSite == null ? openPlacing() : closePlacing();
  const kind = t.dataset.imagery;
  if (kind && globe) {
    globe.setImagery(kind);
    globeBar.querySelectorAll("[data-imagery]").forEach((b) => b.classList.toggle("active", b === t));
  }
});

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
  if (e.target.closest("[data-globe]") && sites.has(selectedId)) { openGlobeView(sites.get(selectedId)); return; }
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

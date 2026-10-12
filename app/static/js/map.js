// Overview: sites (left), map (center), live alarm feed with active timers (right).
import { CFG, api, esc, toast, fmtTime, relTime, timerHtml, on, openAlarms, openCounts, openAck, createMap, pinIcon, markerState, STATUS, PRIORITY, setArmed, armCardHtml, ruleHealthHtml, inView, tenantTag, multiCompany } from "./common.js";
import { openDrawer } from "./drawer.js";

const sites = new Map();     // id -> site
const markers = new Map();   // site id -> Leaflet marker
let selectedId = null, feedPriority = "";
let globe = null, globeOpening = false;   // the 3D view (globe.js), loaded on first use
let plan = null;                           // the top-down view (plan.js): the 2D map on aerial imagery with cameras
const view = () => globe || plan;          // whichever camera view is open; both have the same editor API
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
      ${s.lat != null ? `<button class="btn btn-sm btn-primary" type="button" data-plan title="Aerial view from straight above, with this site's cameras">Top-down view</button>
        <button class="btn btn-sm" type="button" data-globe title="Fly into a 3D aerial view of this site">3D view</button>` : ""}
      <a class="btn btn-sm" href="/alarms?site_id=${s.id}&state=all">Alarm history</a>
      ${CFG.isAdmin ? `<a class="btn btn-sm" href="/sites/${s.id}/edit">Edit site</a>` : ""}
    </div>`;
}

// ---------------------------------------------------------------- right: live feed
const FEED_MAX = 100;
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
  // Draw the top FEED_MAX only (criticals first): a 500-card feed took ~2 s of layout per redraw, which
  // held back the critical pop-up by ~3 s. The tab counts above stay exact.
  const more = rows.length - FEED_MAX;
  ul.innerHTML = rows.slice(0, FEED_MAX).map((a) => `
    <li class="feed-item p${a.priority}" data-id="${a.id}" tabindex="0">
      <div class="feed-top"><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span>${timerHtml(a.event_ts_ms)}</div>
      <div class="title">${esc(a.caption)}</div>
      <div class="feed-where">${tenantTag(a)}<b>${esc(a.site_name)}</b>${a.site_address ? ` · ${esc(a.site_address)}` : ""}</div>
      <div class="meta">${a.source_name ? `${esc(a.source_name)} · ` : ""}${fmtTime(a.event_ts_ms)}</div>
      <div class="feed-actions"><button class="btn btn-sm btn-primary" data-ack="${a.id}">Acknowledge</button>
        <button class="btn btn-sm" data-detail="${a.id}">Details</button></div>
    </li>`).join("") + (more > 0 ? `<li class="empty feed-more">+${more} more open. <a href="/alarms?state=open">See them all on the Alarms page</a></li>` : "");
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
  globe?.update(); plan?.update(); renderHud();
}

function select(id, pan = true) {
  selectedId = id;
  renderAll();
  const s = sites.get(id);
  if (pan && s?.lat != null && plan) plan.flyTo(s);
  else if (pan && s?.lat != null) lmap.flyTo([s.lat, s.lng], Math.max(lmap.getZoom(), 15), { duration: 0.8 });
  if (pan && globe) globe.flyTo(s);
}

// ---------------------------------------------------------------- 3D view (over the map pane)
const $id = (x) => document.getElementById(x);
const mapCanvas = document.querySelector(".map-canvas"), globeEl = $id("globe"), globeBar = $id("globe-bar"), globeMsg = $id("globe-msg"), hud = $id("hud");
const camPanel = $id("cam-panel"), camPlace = $id("cam-place"), camsBtn = $id("globe-cams"), camHover = $id("cam-hover");
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
    ${lat.toFixed(5)}, ${lng.toFixed(5)}<br>ALT ${Math.round(alt).toLocaleString()} M · HDG ${String(Math.round(heading) % 360).padStart(3, "0")}°`;
}
const tickClock = () => { $id("hud-clock").textContent = new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC"; };

async function openGlobeView(site) {
  if (globe) { globe.flyTo(site); return; }
  if (globeOpening) return;
  closePlanView();
  globeOpening = true;
  globeEl.hidden = false; globeBar.hidden = false;
  globeMsg.textContent = "Loading 3D view…"; globeMsg.hidden = false;
  try {
    const { openGlobe } = await import("./globe.js");
    globe = await openGlobe(globeEl, {
      pins: globePins, cameras: globeCams,
      onPick: (id) => select(id),
      onPickCamera: pickCamera,
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
    $id("globe-close").textContent = "2D map";
    $id("imagery-3d").textContent = globe.hasBuildings ? "3D" : "Satellite";
    showImagery("sat");
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

// ---- top-down view: the 2D map itself, on aerial imagery and zoomed into the site, with cameras
// drawn flat (no WebGL, so no graphics check or watchdog needed; see plan.js)
async function openPlanView(site) {
  if (plan) { plan.flyTo(site); return; }
  if (globe) closeGlobeView();
  try {
    const { openPlan } = await import("./plan.js");
    if (plan) return;
    plan = openPlan(lmap, { cameras: globeCams, onPickCamera: pickCamera, onHoverCamera: hoverCamera });
    window.__plan = plan;   // for tools/e2e/topdown.py
  } catch (err) {
    console.error("top-down view failed", err);
    toast(`Top-down view didn't open (${esc(err.message || String(err))}). The map and alarms are unaffected.`, { kind: "error", timeout: 8000 });
    return;
  }
  mapCanvas.classList.add("plan-open");
  globeBar.hidden = false; camsBtn.hidden = !CFG.isAdmin;
  $id("globe-close").textContent = "Exit top-down";
  $id("imagery-3d").textContent = "Satellite";
  showImagery("sat");
  plan.flyTo(sites.get(selectedId) || site);
}

function closePlanView() {
  if (!plan) return;
  hideHover(); closeCamPanel(); closePlacing();
  try { plan.destroy(); } catch (err) { console.error(err); }
  plan = null; delete window.__plan;
  mapCanvas.classList.remove("plan-open");
  globeBar.hidden = true;
}

const showImagery = (kind) => globeBar.querySelectorAll("[data-imagery]").forEach((b) => b.classList.toggle("active", b.dataset.imagery === kind));

// A camera was clicked on either view: edit it while placing that site's cameras, else watch it live.
function pickCamera(siteId, dev) {
  hideHover();
  if (placingSite === siteId) return startCamEdit(dev, sites.get(siteId)?.camera_places?.[dev]?.name || "Camera");
  openCamPanel(siteId, dev);
}

// ---- live preview on hover (top-down view). A short delay so sweeping the mouse across the map
// doesn't open streams; leaving the camera closes it (and its stream) after a moment, so moving
// from the dot to the cone doesn't flicker. Not while placing cameras or for the pinned one.
const HOVER_DELAY_MS = 250, HOVER_GRACE_MS = 200;
let hover = null;     // { key, siteId, deviceId, pt, showTimer, hideTimer, live }
function hoverCamera(siteId, deviceId, pt) {
  const key = `${siteId}:${deviceId}`;
  if (!pt) {
    if (hover?.key === key) { clearTimeout(hover.hideTimer); hover.hideTimer = setTimeout(hideHover, HOVER_GRACE_MS); }
    return;
  }
  if (hover?.key === key) { clearTimeout(hover.hideTimer); return; }
  hideHover();
  if (placingSite != null || (camOpen?.siteId === siteId && camOpen?.deviceId === deviceId)) return;
  hover = { key, siteId, deviceId, pt, showTimer: setTimeout(showHover, HOVER_DELAY_MS) };
}
async function showHover() {
  const h = hover, s = sites.get(h?.siteId), p = s?.camera_places?.[h?.deviceId];
  if (!p) return hideHover();
  const a = camAlarm(h.siteId, h.deviceId);
  camHover.classList.toggle("cam-alarm", !!a);
  camHover.innerHTML = `<div class="cam-head"><span><b>${esc(p.name || "Camera")}</b> <span class="muted">· ${a ? "ALARM" : "click to keep open"}</span></span></div>
    <div class="cam-live"></div>`;
  // Beside the mouse, flipped to stay inside the map.
  const W = 320, H = 222, M = 8, box = mapCanvas.getBoundingClientRect();
  let x = h.pt.x + 18, y = h.pt.y - H - 12;
  if (x + W > box.width - M) x = h.pt.x - 18 - W;
  if (y < M) y = h.pt.y + 18;
  camHover.style.left = `${Math.max(M, Math.min(x, box.width - W - M))}px`;
  camHover.style.top = `${Math.max(M, Math.min(y, box.height - H - M))}px`;
  camHover.hidden = false;
  const { mountLive } = await import("./live.js");
  if (hover !== h) return;
  h.live = mountLive(camHover.querySelector(".cam-live"), { id: 0, device_id: h.deviceId },
    { label: false, poster: true, base: `/media/sites/${h.siteId}/cameras/${encodeURIComponent(h.deviceId)}` });
}
function hideHover() {
  if (!hover) return;
  clearTimeout(hover.showTimer); clearTimeout(hover.hideTimer);
  try { hover.live?.destroy(); } catch (err) { console.error(err); }
  hover = null;
  camHover.hidden = true; camHover.replaceChildren();
}
lmap.on("movestart zoomstart", hideHover);

// ---- live camera window: click a camera on the 3D or top-down view
async function openCamPanel(siteId, deviceId) {
  const s = sites.get(siteId), p = s?.camera_places?.[deviceId];
  if (!p) return;
  closeCamPanel();
  camOpen = { siteId, deviceId };
  const a = camAlarm(siteId, deviceId);
  camPanel.classList.toggle("cam-alarm", !!a);
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

// ---- place and edit cameras (admins). The editor in globe.js / plan.js does the mouse work; this panel
// lists the site's cameras and shows/edits the numbers. Every finished change is saved at once.
let placingSite = null, editing = null;      // editing = { dev, name }
const MODE_HINT = {
  spot: (n) => `Click where ${n} is.`,
  aim: (n) => `Move the mouse to aim ${n}: the cone follows (direction and distance). Click to set it.`,
  edit: () => "Drag the white dot to move, the orange tip to aim and set distance, the yellow side dots to widen or narrow.",
};
const camRow = (c) => `<li data-dev="${esc(c.device_id)}" data-name="${esc(c.name)}">
  <span class="nm" title="${esc(c.name)}">${esc(c.name)}${c.place ? ' <span class="placed">✓ placed</span>' : ""}</span>
  <button type="button" class="go" data-edit>${c.place ? "Edit" : "Place"}</button>
  ${c.place ? '<button type="button" data-unplace title="Take it off the map">Remove</button>' : ""}</li>`;

async function openPlacing() {
  hideHover();
  const s = sites.get(selectedId);
  if (!s) { toast("Select a site first.", { kind: "warn" }); return; }
  placingSite = s.id;
  camsBtn.classList.add("active");
  camPlace.hidden = false;
  if (!camPlace.querySelector("ul")) camPlace.innerHTML = `<div class="hint">Loading ${esc(s.name)}'s cameras…</div>`;
  try {
    const cams = await api(`/api/sites/${s.id}/cameras`);
    if (placingSite !== s.id) return;
    camPlace.innerHTML = `<div class="cam-editor" id="cam-editor" hidden></div>
      <div class="hint">Pick a camera to place it, or click a camera on the map to edit it.</div>
      <ul>${cams.map(camRow).join("") || '<li class="nm">No cameras found on this site.</li>'}</ul>`;
    if (editing) renderEditor();
  } catch (err) {
    camPlace.innerHTML = `<div class="hint">Couldn't list cameras: ${esc(err.message)}</div>`;
  }
}
function closePlacing() {
  stopCamEdit();
  placingSite = null; camsBtn.classList.remove("active");
  camPlace.hidden = true; camPlace.replaceChildren();
}

function renderEditor(st = view()?.editing && editing ? editing.st : null, saveState = editing?.saved || "") {
  const box = $id("cam-editor");
  if (!box || !editing) return;
  box.hidden = false;
  const mode = view()?.editing?.mode || "spot";
  box.innerHTML = `<div class="ed-name"><b>${esc(editing.name)}</b><button type="button" data-ed-done>Done</button></div>
    <div class="ed-hint">${esc(MODE_HINT[mode](editing.name))}</div>
    ${st && mode !== "spot" ? `<div class="ed-nums">
      <label>Aim <input type="number" data-ed="heading" min="0" max="359" step="1" value="${Math.round(st.heading) % 360}">°</label>
      <label>Width <input type="number" data-ed="fov" min="5" max="360" step="1" value="${Math.round(st.fov)}">°</label>
      <label>Distance <input type="number" data-ed="range_m" min="2" max="500" step="1" value="${Math.round(st.range_m)}"> m</label>
      <button type="button" data-ed-dome>${st.fov >= 360 ? "Cone" : "360° dome"}</button>
    </div>` : ""}
    <div class="ed-save" id="ed-save">${esc(saveState)}</div>`;
}
// While dragging only the numbers change (rebuilding the inputs would steal focus).
function paintNumbers(st) {
  const box = $id("cam-editor");
  if (!box) return;
  for (const k of ["heading", "fov", "range_m"]) {
    const el = box.querySelector(`[data-ed="${k}"]`);
    if (el && document.activeElement !== el) el.value = Math.round(k === "heading" ? st[k] % 360 : st[k]);
  }
  const dome = box.querySelector("[data-ed-dome]");
  if (dome) dome.textContent = st.fov >= 360 ? "Cone" : "360° dome";
}

async function saveCam(siteId, dev, place) {
  // Kept on `editing` too: the list refresh after a first placement redraws the editor.
  const say = (t) => { if (editing?.dev === dev) editing.saved = t; const el = $id("ed-save"); if (el) el.textContent = t; };
  say("Saving…");
  try {
    await saveSite(await api(`/api/sites/${siteId}/cameras/${encodeURIComponent(dev)}`, { method: "PUT", body: place }));
    say("Saved ✓");
    const li = camPlace.querySelector(`li[data-dev="${CSS.escape(dev)}"]`);
    if (li && !li.querySelector(".placed")) openPlacing();          // first placement: refresh the list
  } catch (err) { say(`Not saved: ${err.message}`); toast(esc(err.message), { kind: "error" }); }
}

function startCamEdit(dev, name) {
  const site = sites.get(placingSite);
  if (!site || !view()) return;
  hideHover();
  stopCamEdit();
  const place = site.camera_places?.[dev];
  editing = { dev, name, st: place ? { ...place } : null };
  view().editCamera({ siteId: site.id, deviceId: dev, name, ...(place || {}) }, {
    onMode: (mode, st) => { editing.st = st; renderEditor(st); },
    onChange: (st) => { editing.st = st; paintNumbers(st); },
    onCommit: (place) => saveCam(site.id, dev, place),
  });
  renderEditor(editing.st);
  camPlace.querySelectorAll("li[data-dev]").forEach((li) => li.classList.toggle("editing", li.dataset.dev === dev));
}
function stopCamEdit() {
  view()?.stopEditing();
  editing = null;
  const box = $id("cam-editor");
  if (box) { box.hidden = true; box.replaceChildren(); }
  camPlace.querySelectorAll("li.editing").forEach((li) => li.classList.remove("editing"));
}
async function saveSite(data) { sites.set(data.id, { ...sites.get(data.id), ...data }); renderAll(); }

camPlace.addEventListener("click", async (e) => {
  if (!view() || placingSite == null) return;
  if (e.target.closest("[data-ed-done]")) return stopCamEdit();
  if (e.target.closest("[data-ed-dome]") && editing?.st) return editorSet({ fov: editing.st.fov >= 360 ? 90 : 360 });
  const li = e.target.closest("li[data-dev]");
  if (!li) return;
  const dev = li.dataset.dev, name = li.dataset.name;
  if (e.target.closest("[data-unplace]")) {
    if (editing?.dev === dev) stopCamEdit();
    await saveSite(await api(`/api/sites/${placingSite}/cameras/${encodeURIComponent(dev)}`, { method: "DELETE" }));
    return openPlacing();
  }
  if (e.target.closest("[data-edit]")) startCamEdit(dev, name);
});
// Typing a number applies it (and saves) when the field is left or Enter is pressed.
const editorSet = (values) => { if (editing) view()?.currentEditor()?.set(values); };
camPlace.addEventListener("change", (e) => {
  const k = e.target.dataset?.ed;
  if (!k) return;
  const v = Number(e.target.value);
  if (!Number.isFinite(v)) return;
  editorSet({ [k]: k === "heading" ? ((v % 360) + 360) % 360 : v });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && editing && view()?.editing?.mode !== "edit") stopCamEdit();
});

globeBar.addEventListener("click", (e) => {
  const t = e.target;
  if (t.id === "globe-close") return plan ? closePlanView() : closeGlobeView();
  if (t.id === "globe-recenter") return view()?.flyTo(sites.get(selectedId));
  if (t.id === "globe-cams") return placingSite == null ? openPlacing() : closePlacing();
  const kind = t.dataset.imagery;
  if (kind && view()) { view().setImagery(kind); showImagery(kind); }
});

function focusAlarm(a) {
  if (sites.has(a.site_id)) select(a.site_id, !plan);
  if (plan && sites.has(a.site_id)) plan.focusCamera(sites.get(a.site_id), a.device_id);   // top-down open: go to the camera
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
  if (e.target.closest("[data-plan]") && sites.has(selectedId)) { openPlanView(sites.get(selectedId)); return; }
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

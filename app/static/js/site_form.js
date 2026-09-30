import { api, esc, createMap, pinIcon } from "./common.js";

const site = window.SITE;
const $ = (id) => document.getElementById(id);
const fields = ["name", "host", "nx_user", "address", "lat", "lng", "notes"];
const result = $("conn-result");
let pickMap, pin;

if (site) {
  fields.forEach((f) => { if (site[f] !== null && site[f] !== undefined) $(f).value = site[f]; });
  $("host").value = site.cloud_id && site.host.includes(".relay.vmsproxy.com") ? site.cloud_id : site.host;
}

function payload(connect = true) {
  const num = (v) => (v === "" ? null : Number(v));
  return {
    name: $("name").value, host: $("host").value, nx_user: $("nx_user").value, nx_pass: $("nx_pass").value,
    address: $("address").value, lat: num($("lat").value), lng: num($("lng").value), notes: $("notes").value, connect,
  };
}

function showResult(kind, html) { result.innerHTML = `<div class="alert alert-${kind}">${html}</div>`; }

function okHtml(info) {
  return `<b>Connected.</b> NX site <b>${esc(info.nx_site_name || "—")}</b>, version ${esc(info.nx_version || "?")}, ${info.camera_count} camera(s).`;
}

$("test-btn").addEventListener("click", async () => {
  const btn = $("test-btn");
  btn.disabled = true; showResult("warn", "Connecting…");
  try {
    const p = payload();
    const info = await api("/api/sites/test", { method: "POST", body: { host: p.host, nx_user: p.nx_user, nx_pass: p.nx_pass, site_id: site?.id } });
    showResult("ok", okHtml(info));
    if (!$("name").value && info.nx_site_name) $("name").value = info.nx_site_name;
  } catch (e) {
    showResult("error", `<b>Connection failed:</b> ${esc(e.message)}`);
  } finally { btn.disabled = false; }
});

async function save(connect) {
  const btn = $("save-btn");
  btn.disabled = true;
  if (connect) showResult("warn", "Connecting…");
  try {
    await api(site ? `/api/sites/${site.id}` : "/api/sites", { method: site ? "PUT" : "POST", body: payload(connect) });
    location.href = "/sites";
  } catch (e) {
    const offerSkip = e.status === 400 && connect && e.detail?.status;
    showResult("error", `<b>${connect ? "Could not connect" : "Could not save"}:</b> ${esc(e.message)}
      ${offerSkip ? '<div style="margin-top:8px"><button type="button" class="btn btn-sm" id="save-anyway">Save anyway (poller keeps retrying)</button></div>' : ""}`);
    $("save-anyway")?.addEventListener("click", () => save(false));
    btn.disabled = false;
  }
}
$("site-form").addEventListener("submit", (e) => { e.preventDefault(); if (e.target.reportValidity()) save(true); });

// ---------------------------------------------------------------- location picker
function setPin(lat, lng, pan = true) {
  $("lat").value = lat.toFixed(6);
  $("lng").value = lng.toFixed(6);
  if (!pin) {
    pin = L.marker([lat, lng], { icon: pinIcon("ok"), draggable: true, title: "Site location" }).addTo(pickMap);
    pin.on("dragend", () => { const p = pin.getLatLng(); setPin(p.lat, p.lng, false); });
  } else {
    pin.setLatLng([lat, lng]);
  }
  if (pan) pickMap.setView([lat, lng], Math.max(pickMap.getZoom(), 17));
}

function initialView() {
  const lat = parseFloat($("lat").value), lng = parseFloat($("lng").value);
  return Number.isNaN(lat) || Number.isNaN(lng) ? null : [lat, lng];
}

const start = initialView();
pickMap = createMap($("pick-map"), { center: start || [39.8, -98.6], zoom: start ? 17 : 4 });
pickMap.on("click", (e) => setPin(e.latlng.lat, e.latlng.lng, false));
if (start) setPin(start[0], start[1], false);

const geoBox = document.createElement("div");
$("address").closest(".field").appendChild(geoBox);

async function findAddress() {
  const q = $("address").value.trim();
  if (q.length < 3) return;
  const btn = $("geocode-btn");
  btn.disabled = true;
  geoBox.innerHTML = '<small class="muted">Searching…</small>';
  try {
    const results = await api(`/api/geocode?q=${encodeURIComponent(q)}`);
    if (!results.length) { geoBox.innerHTML = '<small class="muted">No match. Try adding city and state, or click the map.</small>'; return; }
    const pick = (r) => { $("address").value = r.label; setPin(r.lat, r.lng); geoBox.innerHTML = ""; };
    if (results.length === 1) return pick(results[0]);
    geoBox.innerHTML = `<small class="muted">Pick the match:</small><div style="display:flex;flex-direction:column;gap:4px;margin-top:4px">${
      results.map((r, i) => `<button type="button" class="btn btn-sm" style="justify-content:flex-start;text-align:left;height:auto;padding:6px 10px" data-geo="${i}">${esc(r.label)}</button>`).join("")}</div>`;
    geoBox.querySelectorAll("[data-geo]").forEach((b) => b.addEventListener("click", () => pick(results[Number(b.dataset.geo)])));
  } catch (e) {
    geoBox.innerHTML = `<small class="muted">Address lookup failed (${esc(e.message)}). Click the map instead.</small>`;
  } finally {
    btn.disabled = false;
  }
}
$("geocode-btn").addEventListener("click", findAddress);
$("address").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); findAddress(); } });
["lat", "lng"].forEach((f) => $(f).addEventListener("change", () => {
  const v = initialView();
  if (v) setPin(v[0], v[1]);
}));

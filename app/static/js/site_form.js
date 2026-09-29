import { CFG, api, esc, loadMaps, mapColorScheme } from "./common.js";

const site = window.SITE;
const $ = (id) => document.getElementById(id);
const fields = ["name", "host", "nx_user", "address", "lat", "lng", "notes"];
const result = $("conn-result");
let maps, pickMap, pin;

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
  if (!pickMap) return;
  const pos = { lat, lng };
  if (!pin) {
    pin = new maps.marker.AdvancedMarkerElement({ map: pickMap, position: pos, gmpDraggable: true, title: "Site location" });
    pin.addListener("dragend", () => { const p = pin.position; setPin(typeof p.lat === "function" ? p.lat() : p.lat, typeof p.lng === "function" ? p.lng() : p.lng, false); });
  } else {
    pin.position = pos;
  }
  if (pan) { pickMap.panTo(pos); if (pickMap.getZoom() < 15) pickMap.setZoom(17); }
}

function buildPicker() {
  const lat = parseFloat($("lat").value), lng = parseFloat($("lng").value);
  const has = !Number.isNaN(lat) && !Number.isNaN(lng);
  pin = null;
  pickMap = new maps.Map($("pick-map"), {
    mapId: CFG.mapId || "DEMO_MAP_ID", center: has ? { lat, lng } : { lat: 39.8, lng: -98.6 }, zoom: has ? 17 : 4,
    streetViewControl: false, mapTypeControl: true, ...mapColorScheme(maps),
  });
  pickMap.addListener("click", (e) => setPin(e.latLng.lat(), e.latLng.lng(), false));
  if (has) setPin(lat, lng, false);
}

$("geocode-btn").addEventListener("click", async () => {
  const address = $("address").value.trim();
  if (!address || !maps) return;
  try {
    const { results } = await new maps.Geocoder().geocode({ address });
    if (!results.length) throw new Error("No match");
    const loc = results[0].geometry.location;
    $("address").value = results[0].formatted_address;
    setPin(loc.lat(), loc.lng());
  } catch (e) {
    showResult("error", `Address lookup failed: ${esc(e.message)}. Check that the Geocoding API is enabled for the key, or click the map instead.`);
  }
});
["lat", "lng"].forEach((f) => $(f).addEventListener("change", () => {
  const lat = parseFloat($("lat").value), lng = parseFloat($("lng").value);
  if (!Number.isNaN(lat) && !Number.isNaN(lng)) setPin(lat, lng);
}));
window.addEventListener("portal:theme", () => { if (maps) buildPicker(); });

try {
  maps = await loadMaps();
  buildPicker();
} catch (e) {
  $("pick-notice").hidden = false;
  $("pick-notice").textContent = `${e.message} Enter latitude/longitude manually.`;
  $("geocode-btn").disabled = true;
}

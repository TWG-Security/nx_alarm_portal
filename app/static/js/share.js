// The guard's location-sharing page (/g/<token>, app/routers/share.py). Sends the browser's position while
// sharing is on: right away when the guard moves, and every few seconds anyway so dispatch sees it's live.
// Stop (or closing the page) tells the portal, which keeps the last position and shows the guard offline.
const { token, everyS } = window.PORTAL;
const $ = (id) => document.getElementById(id);
const KEY = "twg-share-on";                 // reopening the page resumes sharing
const MOVE_M = 10;
let watch = null, last = null, sentAt = 0, sentPos = null, timer = null, lock = null, failures = 0;

const metres = (a, b) => {
  const k = Math.cos(((a.lat + b.lat) / 2) * Math.PI / 180);
  return Math.hypot((b.lat - a.lat) * 111320, (b.lng - a.lng) * 111320 * k);
};
const ago = (ms) => { const s = Math.round((Date.now() - ms) / 1000); return s < 5 ? "just now" : s < 90 ? `${s} s ago` : `${Math.round(s / 60)} min ago`; };

function state(kind, text) {
  const el = $("share-state");
  el.className = `share-state share-state--${kind}`;
  el.textContent = text;
}
function facts() {
  if (!last) { $("share-facts").textContent = watch != null ? "Waiting for the phone's GPS…" : ""; return; }
  $("share-facts").textContent = `Last position ${ago(last.ts)} · accurate to ${Math.round(last.accuracy)} m`
    + (sentAt ? ` · sent ${ago(sentAt)}` : "");
}

async function post(path, body) {
  const r = await fetch(`/api/g/${encodeURIComponent(token)}/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}), credentials: "omit", keepalive: true });
  if (r.status === 404 || r.status === 410) { stop(false); state("off", "This link no longer works. Ask dispatch for a new one."); throw new Error("gone"); }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function send(force = false) {
  if (!last || watch == null) return;
  const moved = !sentPos || metres(sentPos, last) >= MOVE_M;
  if (!force && !moved && Date.now() - sentAt < everyS * 1000) return;
  try {
    await post("pos", { lat: last.lat, lng: last.lng, accuracy: last.accuracy, speed: last.speed, heading: last.heading, ts: last.ts });
    sentAt = Date.now(); sentPos = last; failures = 0;
    state("on", "Sharing your location");
  } catch (e) {
    if (e.message === "gone") return;
    if (++failures >= 2) state("warn", "Can't reach dispatch. Still trying…");
  }
  facts();
}

function onPosition(p) {
  const c = p.coords;
  last = { lat: c.latitude, lng: c.longitude, accuracy: c.accuracy, ts: p.timestamp || Date.now(),
           speed: c.speed != null && c.speed >= 0 ? c.speed : null, heading: c.heading != null && !Number.isNaN(c.heading) ? c.heading : null };
  send();
}
function onError(err) {
  if (err.code === 1) { stop(false); state("off", "Location is blocked for this page. See “Location blocked?” below."); return; }
  state("warn", err.code === 3 ? "Waiting for the phone's GPS…" : "The phone can't find its location right now. Still trying…");
}

async function keepAwake() {
  try { lock = await navigator.wakeLock?.request("screen"); } catch { lock = null; }
}

function start() {
  if (!navigator.geolocation) { state("off", "This browser can't share its location."); return; }
  if (watch != null) return;
  watch = navigator.geolocation.watchPosition(onPosition, onError, { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 });
  timer = setInterval(() => { send(); facts(); }, 1000 * Math.max(2, Math.min(everyS, 5)));
  try { localStorage.setItem(KEY, "1"); } catch { /* not remembered */ }
  keepAwake();
  state("wait", "Starting… allow location if the phone asks");
  $("share-start").hidden = true; $("share-stop").hidden = false;
  facts();
}

function stop(tell = true) {
  if (watch != null) navigator.geolocation.clearWatch(watch);
  watch = null;
  clearInterval(timer); timer = null;
  lock?.release?.().catch(() => {}); lock = null;
  try { localStorage.removeItem(KEY); } catch { /* fine */ }
  if (tell) post("stop").catch(() => {});
  state("off", "Not sharing");
  $("share-start").hidden = false; $("share-stop").hidden = true;
  facts();
}

$("share-start").addEventListener("click", start);
$("share-stop").addEventListener("click", () => stop(true));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && watch != null) { keepAwake(); send(true); }
});
// Closing the page: tell dispatch (best effort; a phone that just dies is shown offline after 3 minutes).
window.addEventListener("pagehide", (e) => {
  if (watch != null && !e.persisted) navigator.sendBeacon?.(`/api/g/${encodeURIComponent(token)}/stop`, new Blob(["{}"], { type: "application/json" }));
});
try { if (localStorage.getItem(KEY) === "1") start(); } catch { /* not remembered */ }

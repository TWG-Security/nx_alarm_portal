// Sharing this phone's location with dispatch (app/routers/share.py), used by the location page (/g/<token>,
// share.js) and the alarm page a dispatch text opens (alarm_link.js: "Share my location now"). Sends right away
// when the phone moves, and every few seconds anyway so dispatch sees it's live. Stop (or closing the page) tells
// the portal, which keeps the last position and shows the guard offline.
const MOVE_M = 10;

const metres = (a, b) => {
  const k = Math.cos(((a.lat + b.lat) / 2) * Math.PI / 180);
  return Math.hypot((b.lat - a.lat) * 111320, (b.lng - a.lng) * 111320 * k);
};
export const ago = (ms) => { const s = Math.round((Date.now() - ms) / 1000); return s < 5 ? "just now" : s < 90 ? `${s} s ago` : `${Math.round(s / 60)} min ago`; };

/**
 * token: the location link's token. onState(kind, text, facts): kind "on" | "wait" | "warn" | "off" | "gone".
 * Returns { start(), stop(tell = true), get active() }.
 */
export function createSharer(token, { everyS = 10, onState = () => {} } = {}) {
  let watch = null, last = null, sentAt = 0, sentPos = null, timer = null, lock = null, failures = 0, kind = "off", text = "Not sharing";
  const facts = () => !last ? (watch != null ? "Waiting for the phone's GPS…" : "")
    : `Last position ${ago(last.ts)} · accurate to ${Math.round(last.accuracy)} m${sentAt ? ` · sent ${ago(sentAt)}` : ""}`;
  const state = (k, t) => { kind = k; text = t; onState(k, t, facts()); };

  async function post(path, body) {
    const r = await fetch(`/api/g/${encodeURIComponent(token)}/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}), credentials: "omit", keepalive: true });
    if (r.status === 404 || r.status === 410) { stop(false); state("gone", "This link no longer works. Ask dispatch for a new one."); throw new Error("gone"); }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  async function send(force = false) {
    if (!last || watch == null) return;
    const moved = !sentPos || metres(sentPos, last) >= MOVE_M;
    if (force || moved || Date.now() - sentAt >= everyS * 1000) {
      try {
        await post("pos", { lat: last.lat, lng: last.lng, accuracy: last.accuracy, speed: last.speed, heading: last.heading, ts: last.ts });
        sentAt = Date.now(); sentPos = last; failures = 0;
        state("on", "Sharing your location");
        return;
      } catch (e) {
        if (e.message === "gone") return;
        if (++failures >= 2) state("warn", "Can't reach dispatch. Still trying…");
      }
    }
    onState(kind, text, facts());
  }

  function onPosition(p) {
    const c = p.coords;
    last = { lat: c.latitude, lng: c.longitude, accuracy: c.accuracy, ts: p.timestamp || Date.now(),
             speed: c.speed != null && c.speed >= 0 ? c.speed : null, heading: c.heading != null && !Number.isNaN(c.heading) ? c.heading : null };
    send();
  }
  function onError(err) {
    if (err.code === 1) { stop(false); state("blocked", "Location is blocked for this page. See “Location blocked?” below to allow it, then tap again."); return; }
    state("warn", err.code === 3 ? "Waiting for the phone's GPS…" : "The phone can't find its location right now. Still trying…");
  }
  async function keepAwake() { try { lock = await navigator.wakeLock?.request("screen"); } catch { lock = null; } }

  function start() {
    if (!navigator.geolocation) { state("off", "This browser can't share its location."); return; }
    if (watch != null) return;
    watch = navigator.geolocation.watchPosition(onPosition, onError, { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 });
    timer = setInterval(() => send(), 1000 * Math.max(2, Math.min(everyS, 5)));
    keepAwake();
    state("wait", "Starting… allow location if the phone asks");
  }
  function stop(tell = true) {
    if (watch != null) navigator.geolocation.clearWatch(watch);
    const was = watch != null;
    watch = null;
    clearInterval(timer); timer = null;
    lock?.release?.().catch(() => {}); lock = null;
    if (tell && was) post("stop").catch(() => {});
    state("off", "Not sharing");
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && watch != null) { keepAwake(); send(true); }
  });
  // Closing the page: tell dispatch (best effort; a phone that just dies is shown offline after 3 minutes).
  window.addEventListener("pagehide", (e) => {
    if (watch != null && !e.persisted) navigator.sendBeacon?.(`/api/g/${encodeURIComponent(token)}/stop`, new Blob(["{}"], { type: "application/json" }));
  });
  return { start, stop, get active() { return watch != null; } };
}

/**
 * Ask for the location right away, inside the tap. Safari (iPhone) only shows its "Allow location?" question for a
 * request made directly in the tap; one made after waiting on the network can be refused without asking.
 * Resolves "ok" | "blocked" | "unavailable".
 */
export function askLocationNow() {
  if (!navigator.geolocation) return Promise.resolve("unavailable");
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(() => resolve("ok"), (e) => resolve(e.code === 1 ? "blocked" : "ok"),
      { enableHighAccuracy: true, maximumAge: 60000, timeout: 30000 });
  });
}

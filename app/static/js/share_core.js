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
 * token: the location link's token, or a function returning a promise of it (the alarm page fetches it, and the
 * location request must still happen inside the tap: Safari refuses one made after waiting on the network).
 * onState(kind, text, facts): kind "on" | "wait" | "warn" | "off" | "blocked" | "gone".
 * Returns { start(), stop(tell = true), get active() }. start() makes exactly one location request (watchPosition):
 * two at once (as an earlier version did) let Safari refuse the second while its prompt for the first was open.
 */
export function createSharer(token, { everyS = 10, onState = () => {} } = {}) {
  let tok = typeof token === "function" ? null : token;
  const getToken = async () => (tok ??= await token());
  let watch = null, last = null, sentAt = 0, sentPos = null, timer = null, lock = null, failures = 0, kind = "off", text = "Not sharing";
  const facts = () => !last ? (watch != null ? "Waiting for the phone's GPS…" : "")
    : `Last position ${ago(last.ts)} · accurate to ${Math.round(last.accuracy)} m${sentAt ? ` · sent ${ago(sentAt)}` : ""}`;
  const state = (k, t) => { kind = k; text = t; onState(k, t, facts()); };

  async function post(path, body) {
    const t = await getToken();
    const r = await fetch(`/api/g/${encodeURIComponent(t)}/${path}`, {
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
    // Safari's timestamp isn't always milliseconds since 1970: use this phone's clock unless it's plausible.
    const ts = Number(p.timestamp) && Math.abs(Date.now() - Number(p.timestamp)) < 600000 ? Number(p.timestamp) : Date.now();
    last = { lat: c.latitude, lng: c.longitude, accuracy: c.accuracy, ts,
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
    if (watch != null && !e.persisted && tok) navigator.sendBeacon?.(`/api/g/${encodeURIComponent(tok)}/stop`, new Blob(["{}"], { type: "application/json" }));
  });
  return { start, stop, get active() { return watch != null; } };
}

/**
 * Whether this page may use the location without asking: "granted" | "denied" | "prompt" | "unknown". Reopening a
 * page resumes sharing only when "granted": Safari silently refuses a request that doesn't come from a tap.
 */
export async function locationPermission() {
  try { return (await navigator.permissions.query({ name: "geolocation" })).state; } catch { return "unknown"; }
}
export const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

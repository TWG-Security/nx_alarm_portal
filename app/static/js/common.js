// Shared helpers + the single SSE connection every page listens on.

const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
export const CFG = window.PORTAL || {};

// ---------------------------------------------------------------- fetch
export async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (method !== "GET") opts.headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/login"; throw new Error("Signed out"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : d?.message || (Array.isArray(d) ? d.map(e => e.msg).join("; ") : `HTTP ${res.status}`);
    const err = new Error(msg);
    err.status = res.status;
    err.detail = d;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------- formatting
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

export function fmtTime(v) {
  if (v === null || v === undefined || v === "") return "—";
  const d = typeof v === "number" ? new Date(v) : new Date(v);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function relTime(v) {
  if (!v) return "never";
  const s = Math.round((Date.now() - (typeof v === "number" ? v : Date.parse(v))) / 1000);
  if (s < 60) return `${Math.max(s, 0)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const PRIORITY = { 1: "Critical", 2: "High", 3: "System" };
export const STATUS = { online: "Online", offline: "Offline", auth_error: "Login failed", pending: "Pending" };

export function toast(msg, { kind = "", timeout = 5000, onClick } = {}) {
  const root = document.getElementById("toast-root");
  if (!root) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = msg;
  if (onClick) el.addEventListener("click", () => { onClick(); el.remove(); });
  root.appendChild(el);
  setTimeout(() => el.remove(), timeout);
}

// ---------------------------------------------------------------- acknowledge dialog
export function openAck(alarm) {
  const dlg = document.getElementById("ack-dialog");
  const note = document.getElementById("ack-note");
  const submit = document.getElementById("ack-submit");
  document.getElementById("ack-summary").textContent =
    `${alarm.caption} — ${alarm.site_name}${alarm.source_name ? " · " + alarm.source_name : ""}`;
  note.value = "";
  return new Promise((resolve) => {
    const cleanup = () => { dlg.removeEventListener("close", onClose); document.getElementById("ack-cancel").onclick = null; };
    const onClose = async () => {
      cleanup();
      if (dlg.returnValue !== "ok") return resolve(null);
      submit.disabled = true;
      try {
        const updated = await api(`/api/alarms/${alarm.id}/ack`, { method: "POST", body: { note: note.value } });
        if (updated.nx_ack_result && !updated.nx_ack_result.ok) {
          toast(`Acknowledged locally, but NX write-back failed: ${esc(updated.nx_ack_result.error)}`, { kind: "error", timeout: 9000 });
        } else {
          toast("Alarm acknowledged");
        }
        resolve(updated);
      } catch (e) {
        toast(esc(e.message), { kind: "error" });
        resolve(null);
      } finally {
        submit.disabled = false;
      }
    };
    dlg.addEventListener("close", onClose);
    document.getElementById("ack-cancel").onclick = () => dlg.close("cancel");
    dlg.returnValue = "";
    dlg.showModal();
    note.focus();
  });
}

// ---------------------------------------------------------------- theme
export const currentTheme = () => document.documentElement.dataset.theme === "light" ? "light" : "dark";
document.getElementById("theme-toggle")?.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("twg-theme", next); } catch (e) { /* storage blocked */ }
  window.dispatchEvent(new CustomEvent("portal:theme", { detail: next }));
});

// ---------------------------------------------------------------- sound
let muted = false;
try { muted = localStorage.getItem("twg-muted") === "1"; } catch (e) { /* ignore */ }
const muteBtn = document.getElementById("mute-toggle");
function paintMute() {
  if (!muteBtn) return;
  muteBtn.querySelector(".sound-on")?.toggleAttribute("hidden", muted);
  muteBtn.querySelector(".sound-off")?.toggleAttribute("hidden", !muted);
  muteBtn.title = muted ? "Unmute alarm sound" : "Mute alarm sound";
}
paintMute();
muteBtn?.addEventListener("click", () => {
  muted = !muted;
  try { localStorage.setItem("twg-muted", muted ? "1" : "0"); } catch (e) { /* ignore */ }
  paintMute();
});

let audioCtx;
document.addEventListener("click", () => { audioCtx ??= new AudioContext(); }, { once: true });
function chime(priority) {
  if (muted || !audioCtx) return;
  const tones = priority === 1 ? [880, 660, 880, 660] : [880, 660];
  tones.forEach((f, i) => {
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = f;
    o.connect(g); g.connect(audioCtx.destination);
    const t = audioCtx.currentTime + i * 0.18;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
    o.start(t); o.stop(t + 0.17);
  });
}

// ---------------------------------------------------------------- live events (SSE)
const handlers = {};
export function on(event, fn) { (handlers[event] ??= []).push(fn); }

let openCount = 0;
const badge = document.getElementById("nav-alarm-count");
function paintBadge() {
  if (!badge) return;
  badge.hidden = openCount === 0;
  badge.textContent = openCount > 99 ? "99+" : String(openCount);
  badge.classList.toggle("pulse", openCount > 0);
}
async function refreshSummary() {
  try {
    const s = await api("/api/summary");
    openCount = s.open_security + s.open_system;
    paintBadge();
  } catch (e) { /* offline; next event retries */ }
}

function connect() {
  const es = new EventSource("/api/events");
  for (const name of ["alarm.new", "alarm.acked", "site.status", "site.updated", "site.removed"]) {
    es.addEventListener(name, (ev) => {
      const data = JSON.parse(ev.data);
      if (name === "alarm.new") {
        openCount++; paintBadge(); chime(data.priority);
        if (!location.pathname.startsWith("/alarms")) {
          toast(`<b>${esc(data.caption)}</b><br><span class="muted">${esc(data.site_name)}${data.source_name ? " · " + esc(data.source_name) : ""}</span>`,
            { kind: "alarm", timeout: 8000, onClick: () => { location.href = `/alarms?open=${data.id}`; } });
        }
      } else if (name === "alarm.acked") {
        openCount = Math.max(0, openCount - 1); paintBadge();
      }
      (handlers[name] || []).forEach((fn) => fn(data));
    });
  }
  // Re-sync after a reconnect so counts can't drift.
  es.addEventListener("open", () => { refreshSummary(); (handlers["reconnect"] || []).forEach((fn) => fn()); });
}
if (CFG.user) { refreshSummary(); connect(); }

// ---------------------------------------------------------------- maps (Leaflet + OpenStreetMap)
// Dark mode darkens the tile layer with a CSS filter (theme.css), so maps never need rebuilding on theme change.
export function createMap(el, opts = {}) {
  const cfg = CFG.map || {};
  const map = L.map(el, { worldCopyJump: true, ...opts });
  L.tileLayer(cfg.tileUrl, { maxZoom: cfg.maxZoom || 19, attribution: cfg.attribution }).addTo(map);
  // Keep tiles correct when the layout around the map changes size.
  new ResizeObserver(() => map.invalidateSize()).observe(el);
  return map;
}

export function pinIcon(state, count = 0, label = "", selected = false) {
  const n = count > 99 ? "99+" : count || "";
  return L.divIcon({
    className: "pin-icon",
    iconSize: [0, 0],
    html: `<div class="pin pin--${state}${selected ? " selected" : ""}"><div class="pin-head"><span>${n}</span></div>${label ? `<div class="pin-label">${esc(label)}</div>` : ""}</div>`,
  });
}

// Marker state rules, mirrored from app/services/serialize.py:site_dict
export function markerState(site, openSecurity, openSystem) {
  if (!site.enabled || site.status === "offline" || site.status === "auth_error") return "offline";
  if (openSecurity > 0) return "alarm";
  if (openSystem > 0) return "warn";
  return "ok";
}

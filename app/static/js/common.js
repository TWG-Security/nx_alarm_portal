// Shared helpers, the open-alarm store, and the single SSE connection every page uses.

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
  return new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function relTime(v) {
  if (!v) return "never";
  const s = Math.round((Date.now() - (typeof v === "number" ? v : Date.parse(v))) / 1000);
  if (s < 60) return `${Math.max(s, 0)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// "Active for" counter: 0:42, 12:05, 3:07:19, 2d 4h
export function fmtDuration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d}d ${h}h`;
  const mmss = `${String(m).padStart(h ? 2 : 1, "0")}:${String(sec).padStart(2, "0")}`;
  return h ? `${h}:${mmss}` : mmss;
}
// Any element with data-since="<epoch ms>" shows a live counter.
export const timerHtml = (since) => `<span class="timer" data-since="${since}" title="Active since ${esc(fmtTime(since))}">${fmtDuration(Date.now() - since)}</span>`;
setInterval(() => {
  const now = Date.now();
  document.querySelectorAll("[data-since]").forEach((el) => { el.textContent = fmtDuration(now - Number(el.dataset.since)); });
}, 1000);

export const PRIORITY = { 1: "Critical", 2: "Alarm", 3: "Warning" };
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

// ---------------------------------------------------------------- events (local bus)
const handlers = {};
export function on(event, fn) { (handlers[event] ??= []).push(fn); }
export function emit(event, data) { (handlers[event] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } }); }

// ---------------------------------------------------------------- open-alarm store
// Every page keeps the full set of unacknowledged alarms so sound, pop-ups, badge and feed agree.
export const openAlarms = new Map();
let storeLoaded = false;
const CATCH_UP_MS = 15 * 60 * 1000;   // alarms newer than this found by a resync are raised like live ones
export async function loadOpenAlarms() {
  const rows = await api("/api/alarms?state=open&limit=500");
  const known = new Set(openAlarms.keys());
  openAlarms.clear();
  rows.forEach((a) => openAlarms.set(a.id, a));
  emit("store", { reason: "load" });
  // Anything that arrived while the live stream was down: sound it / pop it up now.
  if (storeLoaded) {
    rows.filter((a) => !known.has(a.id) && Date.now() - a.event_ts_ms < CATCH_UP_MS)
        .sort((a, b) => a.event_ts_ms - b.event_ts_ms)
        .forEach((a) => emit("alarm.arrived", a));
  }
  storeLoaded = true;
}
export function openCounts(siteId) {
  const c = { 1: 0, 2: 0, 3: 0 };
  for (const a of openAlarms.values()) if (siteId === undefined || a.site_id === siteId) c[a.priority]++;
  return c;
}

// Apply an acknowledged/updated alarm locally (also used right after our own ack call).
export function applyAlarm(a) {
  if (a.state === "new") openAlarms.set(a.id, a); else openAlarms.delete(a.id);
  emit("store", { reason: "change", alarm: a });
}

// ---------------------------------------------------------------- NX rule health (poller.rule_delays)
// Rules with NX's "Interval of action" set hold repeat alarms back by up to that long.
export function ruleHealthHtml(site, { brief = false } = {}) {
  const d = site.rule_delays || [];
  if (brief) {
    if (d.length) return `<span class="chip p3" title="${esc(d.map((r) => `“${r.name}”: ${r.interval_s} s`).join("\n"))}">${d.length} NX ${d.length > 1 ? "rules delay" : "rule delays"} repeats</span>`;
    return site.rules_readable === false ? '<span class="chip" title="The NX account cannot read rules: #tags, #24h and this check don\'t work">Rules unreadable</span>' : "";
  }
  if (site.rules_readable === false) return '<span class="chip">Can\'t read NX rules</span><div class="arm-line">Give the portal\'s NX account rule-read rights (e.g. Power Users) so #tags, #24h and this check work.</div>';
  if (!d.length) return site.rules_readable ? '<span class="chip online">OK</span>' : "—";
  return `<span class="chip p3">${d.length} ${d.length > 1 ? "rules delay" : "rule delays"} repeat alarms</span>` + d.map((r) =>
    `<div class="arm-line">“${esc(r.name)}” holds repeats up to <b>${r.interval_s} s</b>. In NX, turn off its “Interval of action”.</div>`).join("");
}

// ---------------------------------------------------------------- arming (app/services/arming.py)
// "Wed 18:00", or "today 18:00" / "tomorrow 07:00", in the operator's local time.
export function fmtWhen(ms) {
  const d = new Date(ms), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 86_400_000);
  if (days === 0) return `today ${hm}`;
  if (days === 1) return `tomorrow ${hm}`;
  if (days > 1 && days < 7) return `${d.toLocaleDateString([], { weekday: "short" })} ${hm}`;
  return fmtTime(ms);
}
const ARM_SOURCE = { manual: "", schedule: "by schedule", timer: "disarm timer ran out", default: "" };
export const armChip = (site) => site.arming?.armed === false
  ? '<span class="chip disarmed">Disarmed</span>' : '<span class="chip armed">Armed</span>';
// One line: who/what set the state, since when, and what happens next.
export function armLine(site) {
  const a = site.arming;
  if (!a) return "";
  const parts = [];
  if (a.source === "manual") parts.push(`by ${esc(a.by || "operator")}`);
  else if (ARM_SOURCE[a.source]) parts.push(ARM_SOURCE[a.source]);
  if (a.since_ms) parts.push(a.armed ? relTime(a.since_ms) : `for ${timerHtml(a.since_ms)}`);
  if (a.note) parts.push(`“${esc(a.note)}”`);
  if (a.next_ms) parts.push(`${a.next_armed ? "re-arms" : "disarms"} ${esc(fmtWhen(a.next_ms))}${a.next_source === "timer" ? " (timer)" : ""}`);
  else if (!a.armed) parts.push("<b>stays disarmed until someone re-arms it</b>");
  return parts.join(" · ");
}

const DURATIONS = [[30, "In 30 minutes"], [60, "In 1 hour"], [120, "In 2 hours"], [240, "In 4 hours"],
                   [480, "In 8 hours"], [720, "In 12 hours"], [1440, "In 24 hours"]];
// Arm right away (the safe direction); disarm through a dialog with a re-arm time and a note.
export async function setArmed(site, armed) {
  if (armed) {
    try {
      const s = await api(`/api/sites/${site.id}/arm`, { method: "POST", body: {} });
      toast(`<b>${esc(s.name)}</b> armed`);
      emit("site.updated", s);
      return s;
    } catch (e) { toast(esc(e.message), { kind: "error" }); return null; }
  }
  const dlg = document.getElementById("arm-dialog");
  const sel = document.getElementById("arm-duration");
  const note = document.getElementById("arm-note");
  const a = site.arming || {};
  const schedArm = a.next_source === "schedule" && a.next_armed ? a.next_ms : null;
  sel.innerHTML = (schedArm ? `<option value="">At the next scheduled arm (${esc(fmtWhen(schedArm))})</option>` : "")
    + DURATIONS.map(([m, label]) => `<option value="${m}">${label}${schedArm ? " (or the schedule, if sooner)" : ""}</option>`).join("")
    + (schedArm ? "" : '<option value="">Only when someone re-arms it</option>');
  sel.value = schedArm ? "" : "240";
  document.getElementById("arm-title").textContent = `Disarm ${site.name}`;
  document.getElementById("arm-summary").textContent = site.address || "";
  note.value = "";
  return new Promise((resolve) => {
    const onClose = async () => {
      dlg.removeEventListener("close", onClose);
      if (dlg.returnValue !== "ok") return resolve(null);
      const btn = document.getElementById("arm-submit");
      btn.disabled = true;
      try {
        const s = await api(`/api/sites/${site.id}/disarm`, { method: "POST",
          body: { note: note.value, minutes: sel.value ? Number(sel.value) : null } });
        toast(`<b>${esc(s.name)}</b> disarmed${s.arming.next_ms ? `; ${s.arming.next_armed ? "re-arms" : "disarms"} ${esc(fmtWhen(s.arming.next_ms))}` : " until someone re-arms it"}.`, { kind: "warn", timeout: 8000 });
        emit("site.updated", s);
        resolve(s);
      } catch (e) { toast(esc(e.message), { kind: "error" }); resolve(null); }
      finally { btn.disabled = false; }
    };
    dlg.addEventListener("close", onClose);
    document.getElementById("arm-cancel").onclick = () => dlg.close("cancel");
    dlg.returnValue = "";
    dlg.showModal();
    note.focus();
  });
}

// ---------------------------------------------------------------- acknowledge dialog (quick ack)
export function openAck(alarm) {
  const dlg = document.getElementById("ack-dialog");
  const note = document.getElementById("ack-note");
  const submit = document.getElementById("ack-submit");
  document.getElementById("ack-summary").textContent =
    `${alarm.caption} · ${alarm.site_name}${alarm.source_name ? " · " + alarm.source_name : ""}`;
  note.value = "";
  return new Promise((resolve) => {
    const onClose = async () => {
      dlg.removeEventListener("close", onClose);
      if (dlg.returnValue !== "ok") return resolve(null);
      resolve(await acknowledge(alarm.id, note.value, submit));
    };
    dlg.addEventListener("close", onClose);
    document.getElementById("ack-cancel").onclick = () => dlg.close("cancel");
    dlg.returnValue = "";
    dlg.showModal();
    note.focus();
  });
}

export async function acknowledge(id, note, button) {
  if (button) button.disabled = true;
  try {
    const updated = await api(`/api/alarms/${id}/ack`, { method: "POST", body: { note } });
    if (updated.nx_ack_result && !updated.nx_ack_result.ok) {
      toast(`Acknowledged, but the NX write-back failed: ${esc(updated.nx_ack_result.error)}`, { kind: "error", timeout: 9000 });
    } else {
      toast("Alarm acknowledged");
    }
    applyAlarm(updated);
    return updated;
  } catch (e) {
    toast(esc(e.message), { kind: "error" });
    if (e.status === 409) { await loadOpenAlarms().catch(() => {}); }
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------------------------------------------------------------- theme
export const currentTheme = () => document.documentElement.dataset.theme === "light" ? "light" : "dark";
document.getElementById("theme-toggle")?.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("twg-theme", next); } catch (e) { /* storage blocked */ }
  emit("theme", next);
});

// ---------------------------------------------------------------- live events (SSE)
// EventSource gives up for good on an HTTP error (e.g. a 502 while the server restarts), and a
// silently dropped connection can look open forever. So: reconnect with backoff on any failure,
// watch the server's 15 s heartbeat, resync the open-alarm list on every (re)connect and every
// minute, and show the connection state in the top bar.
const HEARTBEAT_TIMEOUT_MS = 12_000;   // server pings every 5 s
const RESYNC_MS = 60_000;              // belt and braces while the stream is healthy
const FALLBACK_POLL_MS = 2_000;        // while the stream is down, poll so alarms still arrive within ~2 s
const LOUD_AFTER_MS = 10_000;          // then shout: banner + tone
let started = false, es = null, lastBeat = 0, backoff = 1000, reconnectTimer = null;
let downSince = null, fallbackTimer = null;
const liveEl = document.getElementById("live-status");
const bannerEl = document.getElementById("conn-banner");

function setDown(down) {
  if (down && downSince === null) {
    downSince = Date.now();
    clearInterval(fallbackTimer);
    fallbackTimer = setInterval(() => {
      loadOpenAlarms().catch(() => {});
      const loud = Date.now() - downSince >= LOUD_AFTER_MS;
      if (bannerEl) bannerEl.hidden = !loud;
      emit("connection", { down: true, loud });
    }, FALLBACK_POLL_MS);
  } else if (!down && downSince !== null) {
    downSince = null;
    clearInterval(fallbackTimer); fallbackTimer = null;
    if (bannerEl) bannerEl.hidden = true;
    emit("connection", { down: false, loud: false });
  }
}

function paintLive(state) {
  setDown(state === "down");
  if (!liveEl) return;
  liveEl.className = `live-status ${state === "ok" ? "ok" : state === "down" ? "down" : ""}`;
  liveEl.textContent = state === "ok" ? "Live" : state === "down" ? "Reconnecting…" : "Connecting…";
  liveEl.title = state === "ok" ? "Receiving live alarm updates"
    : "Live updates are interrupted. Alarms may be delayed until the connection is back";
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  es?.close();
  paintLive("down");
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, backoff);
  backoff = Math.min(backoff * 2, 15_000);
}

const EVENTS = ["alarm.new", "alarm.acked", "alarm.updated", "alarms.reload", "site.status", "site.updated", "site.removed", "site.push"];
function connect() {
  es = new EventSource("/api/events");
  lastBeat = Date.now();
  es.addEventListener("open", () => {
    backoff = 1000;
    lastBeat = Date.now();
    paintLive("ok");
    loadOpenAlarms().catch(() => {});
    emit("reconnect");
  });
  es.addEventListener("error", () => {
    // CLOSED = the browser gave up (HTTP error); CONNECTING = it is retrying itself.
    if (es.readyState === EventSource.CLOSED) scheduleReconnect(); else paintLive("down");
  });
  es.addEventListener("ping", () => { lastBeat = Date.now(); });
  for (const name of EVENTS) {
    es.addEventListener(name, async (ev) => {
      lastBeat = Date.now();
      const data = JSON.parse(ev.data);
      if (name === "alarm.new") {
        const known = openAlarms.has(data.id);
        openAlarms.set(data.id, data);
        emit("store", { reason: "new", alarm: data });
        if (!known) emit("alarm.arrived", data);
      } else if (name === "alarm.acked" || name === "alarm.updated") {
        applyAlarm(data);
      } else if (name === "alarms.reload") {
        await loadOpenAlarms().catch(() => {});
      }
      emit(name, data);
    });
  }
}

export function startLive() {
  if (started || !CFG.user) return;
  started = true;
  paintLive("connecting");
  connect();
  setInterval(() => { if (Date.now() - lastBeat > HEARTBEAT_TIMEOUT_MS) scheduleReconnect(); }, 5000);
  setInterval(() => { loadOpenAlarms().catch(() => {}); }, RESYNC_MS);
  // Laptops waking from sleep: check right away instead of waiting for the watchdog.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && Date.now() - lastBeat > 20_000) scheduleReconnect();
  });
}

// ---------------------------------------------------------------- maps (Leaflet + OpenStreetMap)
// Dark mode darkens the tile layer with a CSS filter (theme.css), so maps never need rebuilding on theme change.
export function createMap(el, opts = {}) {
  const cfg = CFG.map || {};
  const map = L.map(el, { worldCopyJump: true, ...opts });
  L.tileLayer(cfg.tileUrl, { maxZoom: cfg.maxZoom || 19, attribution: cfg.attribution }).addTo(map);
  new ResizeObserver(() => map.invalidateSize()).observe(el);
  return map;
}

export function pinIcon(state, count = 0, label = "", selected = false, disarmed = false) {
  const n = count > 99 ? "99+" : count || "";
  return L.divIcon({
    className: "pin-icon",
    iconSize: [0, 0],
    html: `<div class="pin pin--${state}${selected ? " selected" : ""}${disarmed ? " disarmed" : ""}"><div class="pin-head"><span>${n}</span></div>${label ? `<div class="pin-label">${esc(label)}${disarmed ? ' <span class="pin-tag">DISARMED</span>' : ""}</div>` : ""}</div>`,
  });
}

// Marker state rules, mirrored from app/services/serialize.py:site_dict. counts = {1: critical, 2: alarm, 3: warning}
export function markerState(site, counts) {
  if (!site.enabled || site.status === "offline" || site.status === "auth_error") return "offline";
  if (counts[1] || counts[2]) return "alarm";
  if (counts[3]) return "warn";
  return "ok";
}

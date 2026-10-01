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
  if (res.status === 401) { signedOut(); throw new Error("Signed out"); }
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
export const can = (perm) => (CFG.perms || []).includes(perm);

// ---------------------------------------------------------------- companies (app/scope.py)
// The open-alarm store holds the companies in view plus your own; what's shown follows the view,
// while sound and critical pop-ups follow only your own company's alarms.
const SCOPE = CFG.scope || { mode: "own", tenantIds: null };
export const isMine = (a) => a.tenant_id === undefined || a.tenant_id === CFG.tenantId;
export const inView = (a) => SCOPE.mode === "all" || !SCOPE.tenantIds || a.tenant_id === undefined || SCOPE.tenantIds.includes(a.tenant_id);
export const multiCompany = SCOPE.mode === "all";
// A small company label, shown wherever several companies can appear at once.
export const tenantTag = (x) => (multiCompany && x?.tenant_name ? `<span class="chip tenant-chip" title="Company">${esc(x.tenant_name)}</span>` : "");
// The operator's call on an event (app/services/ack.py VERDICTS).
export const VERDICT = { real: "Real event", false: "False alarm" };
export const verdictChip = (a) => a.verdict ? `<span class="chip verdict-${a.verdict}" title="${a.verdict_by ? `Marked by ${esc(a.verdict_by)}` : ""}">${VERDICT[a.verdict]}</span>` : "";
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
  const rows = await api("/api/alarms?state=open&limit=500&alerting=1");
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
// Open alarms in view (optionally for one site).
export function openCounts(siteId) {
  const c = { 1: 0, 2: 0, 3: 0 };
  for (const a of openAlarms.values()) if (inView(a) && (siteId === undefined || a.site_id === siteId)) c[a.priority]++;
  return c;
}
// Open alarms of your own company: these drive the siren and chime.
export function mineCounts() {
  const c = { 1: 0, 2: 0, 3: 0 };
  for (const a of openAlarms.values()) if (isMine(a)) c[a.priority]++;
  return c;
}

// Apply an acknowledged/updated alarm locally (also used right after our own ack call).
export function applyAlarm(a) {
  if (a.state === "new") openAlarms.set(a.id, a); else openAlarms.delete(a.id);
  emit("store", { reason: "change", alarm: a });
}

// ---------------------------------------------------------------- NX rule health (poller.rule_delays)
// NX's "Interval of action" merges repeat events and logs them when the interval ends, so a
// repeat reaches the portal up to that late (the first event always comes straight through).
const dur = (s) => (s >= 3600 ? `${+(s / 3600).toFixed(1)} h` : s >= 60 ? `${+(s / 60).toFixed(1)} min` : `${s} s`);
export function ruleHealthHtml(site, { brief = false } = {}) {
  const d = site.rule_delays || [], sys = site.system_delays || { count: 0 };
  if (site.rules_readable === false) {
    return brief ? '<span class="chip" title="The portal\'s NX account can\'t read rules, so #tags, #24h and the repeat-delay check don\'t work">Rules unreadable</span>'
      : '<span class="chip">Can\'t read NX rules</span><div class="arm-line">Give the portal\'s NX account rule-read rights (e.g. Power Users) so #tags, #24h and this check work.</div>';
  }
  if (brief) {
    return d.length ? `<span class="chip p3" title="${esc(d.map((r) => `${r.name}: repeats up to ${dur(r.interval_s)} late`).join("\n"))}">Repeat alarms delayed (${d.length})</span>` : "";
  }
  if (site.rules_readable == null) return "—";
  const sysLine = sys.count ? `<div class="arm-line">System events (${sys.count} types): repeats held up to ${dur(sys.max_s)}${sys.worst ? ` (${esc(sys.worst)})` : ""} by NX's default notification rules. Usually fine.</div>` : "";
  if (!d.length) return `<span class="chip online">Security alarms on time</span>${sysLine}`;
  const shown = d.slice(0, 5).map((r) => `<div class="arm-line">${esc(r.name)}: repeats up to <b>${dur(r.interval_s)}</b> late</div>`).join("");
  return `<span class="chip p3">Repeat alarms delayed</span>${shown}${d.length > 5 ? `<div class="arm-line">…and ${d.length - 5} more</div>` : ""}
    <div class="arm-line">Fix: in NX, add a rule for the same event with action <b>Write to log</b> and <b>Interval of action</b> off. Your existing notification rules can keep their limits.</div>${sysLine}`;
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
const nextHtml = (a) => a.next_ms
  ? `${a.next_armed ? "Re-arms" : "Disarms"} ${esc(fmtWhen(a.next_ms))}${a.next_source === "timer" ? " (timer)" : a.next_source === "schedule" ? " (schedule)" : ""}`
  : a.armed ? "" : "<b>Stays disarmed until someone re-arms it</b>";
// Who disarmed it and why. Armed sites only say how they got armed if it wasn't the default.
function whoHtml(a) {
  if (a.armed) return a.source === "default" ? "" : `Armed ${a.source === "manual" ? `by ${esc(a.by || "operator")} ` : `${ARM_SOURCE[a.source]} `}${relTime(a.since_ms)}`;
  const who = a.source === "manual" ? `by ${esc(a.by || "operator")}` : ARM_SOURCE[a.source] || "";
  return [who, a.note ? `“${esc(a.note)}”` : ""].filter(Boolean).join(" · ");
}
// One line for tables: who/why, and what happens next.
export function armLine(site) {
  const a = site.arming;
  if (!a) return "";
  return [a.armed ? "" : `for ${timerHtml(a.since_ms)}`, whoHtml(a), nextHtml(a)].filter(Boolean).join(" · ");
}

const SHIELD = '<path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6l-8-3z"/>';
const ICON_ARMED = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">${SHIELD}<path d="m8.5 12 2.5 2.5 4.5-5"/></svg>`;
const ICON_DISARMED = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">${SHIELD}<path d="M9 9l6 6m0-6-6 6"/></svg>`;
// The site panel's arming card: state at a glance, why, what's next, and one clear button.
export function armCardHtml(site) {
  const a = site.arming;
  if (!a) return "";
  const lines = [whoHtml(a), nextHtml(a)].filter(Boolean);
  return `<div class="arm-card ${a.armed ? "is-armed" : "is-disarmed"}">
    <div class="arm-card-head">${a.armed ? ICON_ARMED : ICON_DISARMED}
      <span class="arm-card-state">${a.armed ? "Armed" : "Disarmed"}</span>
      ${a.armed ? "" : `<span class="arm-card-for" title="Disarmed for">${timerHtml(a.since_ms)}</span>`}</div>
    ${lines.map((l) => `<div class="arm-card-line">${l}</div>`).join("")}
    ${a.armed ? "" : '<div class="arm-card-line">Security events are recorded, not raised.</div>'}
    <button class="btn ${a.armed ? "" : "btn-primary"} arm-card-btn" data-arm="${a.armed ? "disarm" : "arm"}">${a.armed ? "Disarm site…" : "Arm site now"}</button>
  </div>`;
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
  document.getElementById("ack-summary").textContent =
    `${alarm.caption} · ${alarm.site_name}${alarm.source_name ? " · " + alarm.source_name : ""}`;
  note.value = "";
  return new Promise((resolve) => {
    const onClose = async () => {
      dlg.removeEventListener("close", onClose);
      if (!(dlg.returnValue in VERDICT)) return resolve(null);
      resolve(await acknowledge(alarm.id, note.value, null, dlg.returnValue));
    };
    dlg.addEventListener("close", onClose);
    document.getElementById("ack-cancel").onclick = () => dlg.close("cancel");
    dlg.returnValue = "";
    dlg.showModal();
    note.focus();
  });
}

// verdict: "real" | "false" — every acknowledgement records the operator's call.
export async function acknowledge(id, note, button, verdict = "") {
  if (button) button.disabled = true;
  try {
    const updated = await api(`/api/alarms/${id}/ack`, { method: "POST", body: { note, verdict } });
    const what = verdict ? ` as ${VERDICT[verdict].toLowerCase()}` : "";
    if (updated.nx_ack_result && !updated.nx_ack_result.ok) {
      toast(`Acknowledged${what}, but the NX write-back failed: ${esc(updated.nx_ack_result.error)}`, { kind: "error", timeout: 9000 });
    } else {
      toast(`Alarm acknowledged${what}`);
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

// ---------------------------------------------------------------- company switcher (TWG staff)
async function setScope(value) {
  try { await api("/api/scope", { method: "POST", body: { scope: String(value) } }); location.href = location.pathname === "/companies" ? "/companies" : "/"; }
  catch (e) { toast(esc(e.message), { kind: "error" }); }
}
const switcher = document.getElementById("scope-switch");
if (switcher) {
  const current = switcher.value;
  api("/api/tenants").then((rows) => {
    switcher.innerHTML = rows.map((t) => `<option value="${t.own ? "own" : t.id}">${esc(t.label)}${t.own ? " (yours)" : t.is_active ? "" : " (sign-in off)"}</option>`).join("")
      + '<option value="all">All companies</option>';
    switcher.value = current;
  }).catch(() => {});
  switcher.addEventListener("change", () => setScope(switcher.value));
}
document.getElementById("scope-exit")?.addEventListener("click", () => setScope("own"));

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
  if (signedOutAt !== null) return;
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

// Signed out (revoked elsewhere, expired, "sign out everywhere"): never quietly. The page stays, marked
// as not live, with a red banner and the connection tone every 10 s until someone signs in again.
let signedOutAt = null;
export function signedOut() {
  if (signedOutAt !== null) return;
  signedOutAt = Date.now();
  es?.close();
  clearTimeout(reconnectTimer); reconnectTimer = null;
  clearInterval(fallbackTimer); fallbackTimer = null;
  const banner = document.getElementById("signedout-banner");
  if (banner) {
    banner.hidden = false;
    const link = banner.querySelector("a");
    if (link) link.href = `/login?reason=signed_out&next=${encodeURIComponent(location.pathname + location.search)}`;
  }
  if (bannerEl) bannerEl.hidden = true;
  document.body.classList.add("signed-out");
  if (liveEl) { liveEl.className = "live-status down"; liveEl.textContent = "Signed out"; liveEl.title = "Not receiving alarms: sign in again"; }
  emit("signedout");
  const shout = () => emit("connection", { down: true, loud: true });
  shout();
  setInterval(shout, 2000);              // sound.js plays the tone at most every 10 s
}

function scheduleReconnect() {
  if (signedOutAt !== null) return;
  if (reconnectTimer) return;
  es?.close();
  paintLive("down");
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, backoff);
  backoff = Math.min(backoff * 2, 15_000);
}

const EVENTS = ["alarm.new", "alarm.acked", "alarm.updated", "alarm.note", "alarms.reload", "site.status", "site.updated", "site.removed", "site.push"];
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
  es.addEventListener("signedout", () => signedOut());
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

// Optional idle sign-out (Platform page; off by default because a wall screen is never "used").
function watchIdle(minutes) {
  let last = Date.now();
  const touch = () => { last = Date.now(); };
  ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"].forEach((e) => addEventListener(e, touch, { passive: true }));
  setInterval(async () => {
    if (signedOutAt !== null || Date.now() - last < minutes * 60_000) return;
    signedOutAt = Date.now();
    await fetch("/logout", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf } }).catch(() => {});
    location.href = "/login?reason=idle";
  }, 15_000);
}

export function startLive() {
  if (started || !CFG.user) return;
  started = true;
  if (CFG.idleMin > 0) watchIdle(CFG.idleMin);
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

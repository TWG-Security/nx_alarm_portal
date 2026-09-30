import { api, esc, fmtTime, relTime, timerHtml, on, openAck, PRIORITY } from "./common.js";
import { openDrawer } from "./drawer.js";

const params = new URLSearchParams(location.search);
const filt = {
  state: params.get("state") || "open",
  site_id: params.get("site_id") || "",
  priority: params.get("priority") || "",
  q: params.get("q") || "",
};
const PAGE = 100;
let rows = [], oldestId = null;
const listEl = document.getElementById("alarm-list");

function syncUrl() {
  const p = new URLSearchParams();
  Object.entries(filt).forEach(([k, v]) => { if (v && !(k === "state" && v === "open")) p.set(k, v); });
  history.replaceState(null, "", `/alarms${p.toString() ? "?" + p : ""}`);
}

function matches(a) {
  if (filt.state === "open" && a.state !== "new") return false;
  if (filt.state === "acknowledged" && a.state !== "acknowledged") return false;
  if (filt.site_id && String(a.site_id) !== filt.site_id) return false;
  if (filt.priority && String(a.priority) !== filt.priority) return false;
  if (filt.q) {
    const q = filt.q.toLowerCase();
    if (![a.caption, a.source_name, a.description].some((v) => (v || "").toLowerCase().includes(q))) return false;
  }
  return true;
}

function rowHtml(a) {
  const acked = a.state === "acknowledged";
  return `
  <li class="alarm p${a.priority} ${acked ? "acked" : ""}" data-id="${a.id}" tabindex="0">
    <span class="stripe"></span>
    <div>
      <div class="title">${esc(a.caption)}</div>
      <div class="meta">
        <span><b>${esc(a.site_name)}</b></span>
        ${a.source_name ? `<span>${esc(a.source_name)}</span>` : ""}
        <span>${fmtTime(a.event_ts_ms)}</span>
        ${acked ? `<span>Acknowledged by ${esc(a.acked_by || "—")} · ${relTime(a.acked_at)}</span>` : ""}
      </div>
    </div>
    <div class="right">
      <span class="chip p${a.priority}">${PRIORITY[a.priority]}</span>
      ${acked ? '<span class="chip acked">Acknowledged</span>' : `${timerHtml(a.event_ts_ms)}<button class="btn btn-sm btn-primary" data-ack="${a.id}">Acknowledge</button>`}
    </div>
  </li>`;
}

function render() {
  listEl.innerHTML = rows.length ? rows.map(rowHtml).join("")
    : `<li class="empty">${filt.state === "open" ? "No open alarms. All clear." : "No alarms match these filters."}</li>`;
  document.getElementById("alarm-count").textContent = rows.length ? `${rows.length}${oldestId ? "+" : ""} shown` : "";
}

async function load(append = false) {
  const p = new URLSearchParams({ state: filt.state, limit: PAGE });
  ["site_id", "priority", "q"].forEach((k) => { if (filt[k]) p.set(k, filt[k]); });
  if (append && oldestId) p.set("before_id", oldestId);
  const data = await api(`/api/alarms?${p}`);
  rows = append ? rows.concat(data) : data;
  oldestId = data.length === PAGE ? data[data.length - 1].id : null;
  document.getElementById("load-more").hidden = !oldestId;
  render();
}

function upsert(a) {
  const i = rows.findIndex((r) => r.id === a.id);
  if (i >= 0) { if (matches(a)) rows[i] = a; else rows.splice(i, 1); render(); }
  else if (matches(a)) { rows.unshift(a); render(); listEl.querySelector(`[data-id="${a.id}"]`)?.classList.add("fresh"); }
}

listEl.addEventListener("click", async (e) => {
  const ackId = e.target.dataset.ack;
  if (ackId) { e.stopPropagation(); await openAck(rows.find((r) => r.id === Number(ackId))); return; }
  const li = e.target.closest(".alarm");
  if (li) openDrawer(Number(li.dataset.id), rows.find((r) => r.id === Number(li.dataset.id)));
});
listEl.addEventListener("keydown", (e) => {
  const li = e.target.closest(".alarm");
  if (li && e.key === "Enter") openDrawer(Number(li.dataset.id));
});
document.getElementById("load-more").addEventListener("click", () => load(true));

document.querySelectorAll("#state-seg button").forEach((b) => {
  b.classList.toggle("active", b.dataset.state === filt.state);
  b.addEventListener("click", () => {
    filt.state = b.dataset.state;
    document.querySelectorAll("#state-seg button").forEach((x) => x.classList.toggle("active", x === b));
    syncUrl(); load();
  });
});
const siteSel = document.getElementById("f-site");
const prioSel = document.getElementById("f-priority");
const qIn = document.getElementById("f-q");
prioSel.value = filt.priority;
qIn.value = filt.q;
siteSel.addEventListener("change", () => { filt.site_id = siteSel.value; syncUrl(); load(); });
prioSel.addEventListener("change", () => { filt.priority = prioSel.value; syncUrl(); load(); });
let qTimer;
qIn.addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => { filt.q = qIn.value.trim(); syncUrl(); load(); }, 300); });

on("store", ({ alarm } = {}) => { if (alarm) upsert(alarm); });
on("alarm.arrived", upsert);   // includes alarms caught up by a resync while the live stream was down
on("alarms.reload", () => load().catch(() => {}));
on("reconnect", () => load().catch(() => {}));

api("/api/sites").then((sites) => {
  siteSel.innerHTML = '<option value="">All sites</option>' + sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  siteSel.value = filt.site_id;
}).catch(() => {});
await load().catch((e) => { listEl.innerHTML = `<li class="empty">${esc(e.message)}</li>`; });
const openId = Number(params.get("open"));
if (openId) openDrawer(openId);

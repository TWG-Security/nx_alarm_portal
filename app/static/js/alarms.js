import { api, esc, fmtTime, relTime, timerHtml, on, openAck, toast, PRIORITY, VERDICT, verdictChip, can } from "./common.js";
import { openDrawer } from "./drawer.js";

const params = new URLSearchParams(location.search);
const filt = {
  state: params.get("state") || "open",
  site_id: params.get("site_id") || "",
  priority: params.get("priority") || "",
  q: params.get("q") || "",
  verdict: params.get("verdict") || "",
};
const BULK = can("alarms.bulk_edit");    // admin override: bulk verdicts / acknowledgements
const selected = new Set();
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
  if ((filt.state === "acknowledged" || filt.state === "disarmed") && a.state !== filt.state) return false;
  if (filt.site_id && String(a.site_id) !== filt.site_id) return false;
  if (filt.priority && String(a.priority) !== filt.priority) return false;
  if (filt.verdict && (a.verdict || "none") !== filt.verdict) return false;
  if (filt.q) {
    const q = filt.q.toLowerCase();
    if (![a.caption, a.source_name, a.description].some((v) => (v || "").toLowerCase().includes(q))) return false;
  }
  return true;
}

function rowHtml(a) {
  const acked = a.state === "acknowledged";
  const box = BULK ? `<input type="checkbox" class="sel" data-sel="${a.id}" ${selected.has(a.id) ? "checked" : ""} aria-label="Select alarm ${a.id}">` : "";
  if (a.state === "disarmed") return `
  <li class="alarm p${a.priority} acked${BULK ? " has-sel" : ""}" data-id="${a.id}" tabindex="0">
    <span class="stripe"></span>${box}
    <div>
      <div class="title">${esc(a.caption)}</div>
      <div class="meta"><span><b>${esc(a.site_name)}</b></span>${a.source_name ? `<span>${esc(a.source_name)}</span>` : ""}<span>${fmtTime(a.event_ts_ms)}</span></div>
    </div>
    <div class="right">${verdictChip(a)}<span class="chip p${a.priority}">${PRIORITY[a.priority]}</span><span class="chip disarmed" title="Received while the site was disarmed, so it was recorded but not raised">Site disarmed</span></div>
  </li>`;
  return `
  <li class="alarm p${a.priority} ${acked ? "acked" : ""}${BULK ? " has-sel" : ""}" data-id="${a.id}" tabindex="0">
    <span class="stripe"></span>${box}
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
      ${verdictChip(a)}<span class="chip p${a.priority}">${PRIORITY[a.priority]}</span>
      ${acked ? '<span class="chip acked">Acknowledged</span>' : `${timerHtml(a.event_ts_ms)}<button class="btn btn-sm btn-primary" data-ack="${a.id}">Acknowledge</button>`}
    </div>
  </li>`;
}

function render() {
  listEl.innerHTML = rows.length ? rows.map(rowHtml).join("")
    : `<li class="empty">${filt.state === "open" ? "No open alarms. All clear." : filt.state === "disarmed" ? "No events were received while a site was disarmed." : "No alarms match these filters."}</li>`;
  document.getElementById("alarm-count").textContent = rows.length ? `${rows.length}${oldestId ? "+" : ""} shown` : "";
  renderBulk();
}

// ---------------------------------------------------------------- bulk edit (alarms.bulk_edit)
function renderBulk() {
  if (!BULK) return;
  for (const id of [...selected]) if (!rows.some((r) => r.id === id)) selected.delete(id);
  const bar = document.getElementById("bulk-bar");
  bar.hidden = false;
  const n = selected.size;
  document.getElementById("bulk-count").textContent = n ? `${n} selected` : "Select alarms to mark them in bulk";
  bar.querySelectorAll("[data-bulk]").forEach((b) => { b.disabled = !n; });
  const all = document.getElementById("bulk-all");
  all.checked = rows.length > 0 && rows.every((r) => selected.has(r.id));
  all.indeterminate = n > 0 && !all.checked;
}

async function bulk(verdict) {
  const picked = rows.filter((r) => selected.has(r.id));
  const open = picked.filter((r) => r.state === "new");
  const crit = open.filter((r) => r.priority === 1).length;
  const change = picked.filter((r) => r.state !== "new" && r.verdict !== verdict).length;
  const same = picked.length - open.length - change;
  const dlg = document.getElementById("bulk-dialog");
  document.getElementById("bulk-title").textContent = `Mark ${picked.length} alarm${picked.length > 1 ? "s" : ""} as ${VERDICT[verdict].toLowerCase()}`;
  document.getElementById("bulk-summary").innerHTML = [
    open.length ? `<b>Acknowledges ${open.length} open alarm${open.length > 1 ? "s" : ""}</b>${crit ? ` <span class="chip p1">${crit} critical</span>` : ""}. Their sound and pop-ups stop for every operator.` : "",
    change ? `Changes the verdict on ${change} closed alarm${change > 1 ? "s" : ""}.` : "",
    same ? `<span class="muted">${same} already marked ${VERDICT[verdict].toLowerCase()} (no change).</span>` : "",
    "Every change is written to the audit log under your name.",
  ].filter(Boolean).join("<br>");
  document.getElementById("bulk-note").value = "";
  dlg.returnValue = "";
  dlg.showModal();
  await new Promise((r) => dlg.addEventListener("close", r, { once: true }));
  if (dlg.returnValue !== "ok") return;
  try {
    const res = await api("/api/alarms/bulk", { method: "POST",
      body: { ids: picked.map((r) => r.id), verdict, note: document.getElementById("bulk-note").value } });
    toast(`Done: ${res.acknowledged.length} acknowledged, ${res.changed.length} changed${res.unchanged.length ? `, ${res.unchanged.length} unchanged` : ""}`);
    selected.clear();
    await load();
  } catch (e) { toast(esc(e.message), { kind: "error" }); }
}

if (BULK) {
  document.getElementById("bulk-cancel").addEventListener("click", () => document.getElementById("bulk-dialog").close());
  document.getElementById("bulk-all").addEventListener("change", (e) => {
    rows.forEach((r) => (e.target.checked ? selected.add(r.id) : selected.delete(r.id)));
    render();
  });
  document.getElementById("bulk-bar").addEventListener("click", (e) => {
    const b = e.target.dataset.bulk;
    if (b === "clear") { selected.clear(); render(); } else if (b) bulk(b);
  });
}

async function load(append = false) {
  const p = new URLSearchParams({ state: filt.state, limit: PAGE });
  ["site_id", "priority", "q", "verdict"].forEach((k) => { if (filt[k]) p.set(k, filt[k]); });
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
  if (e.target.matches(".sel")) {
    const id = Number(e.target.dataset.sel);
    if (e.target.checked) selected.add(id); else selected.delete(id);
    e.stopPropagation(); renderBulk(); return;
  }
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
const verdictSel = document.getElementById("f-verdict");
verdictSel.value = filt.verdict;
verdictSel.addEventListener("change", () => { filt.verdict = verdictSel.value; syncUrl(); load(); });
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

// Camera health page: every camera's last checked picture with boxes around what's wrong, Reference | Now per
// camera, Check now, Accept view / Mute / settings (administrators). Live over the `camera.health` event.
import { api, CFG, esc, fmtTime, on, relTime, tenantTag, toast } from "./common.js";

const grid = document.getElementById("hc-grid");
const showSel = document.getElementById("hf-show");
const siteSel = document.getElementById("hf-site");
const q = document.getElementById("hf-q");
const summaryEl = document.getElementById("hc-summary");
const metaEl = document.getElementById("hc-meta");
const dlg = document.getElementById("hc-dialog");
const params = new URLSearchParams(location.search);
let data = { cameras: [], summary: {} };
let open = null;               // {site_id, device_id} in the dialog
let reloadTimer = null;

const STATUS = { problem: "Problem", warning: "Warning", ok: "OK", offline: "Offline in NX", pending: "Not checked yet" };
const key = (c) => `${c.site_id}:${c.device_id}`;
const imgUrl = (c, kind, t) => `/media/camera-health/${c.site_id}/${encodeURIComponent(c.device_id)}/${kind}.jpg?t=${t || 0}`;
const shown = (c) => (c.muted && (c.status === "problem" || c.status === "warning") ? "muted" : c.status);

// Boxes and the "moved" outline over a picture: percentages, so they follow the picture at any size.
function overlay(issues) {
  const boxes = [], polys = [];
  for (const i of issues || []) {
    for (const b of i.boxes || []) {
      if (b[2] >= 0.999 && b[3] >= 0.999) {               // the whole view: a frame inside the edge
        boxes.push(`<div class="hc-box hc-box--${i.severity} hc-box--whole"><span>${esc(i.title)}</span></div>`);
        continue;
      }
      const top = b[1] < 0.08;                            // label inside the box when it would leave the picture
      boxes.push(`<div class="hc-box hc-box--${i.severity}${top ? " hc-box--label-in" : ""}" style="left:${b[0] * 100}%;top:${b[1] * 100}%;width:${b[2] * 100}%;height:${b[3] * 100}%"><span>${esc(i.title)}</span></div>`);
    }
    if (i.poly) polys.push(`<polygon points="${i.poly.map((p) => p.join(",")).join(" ")}" />`);
  }
  const svg = polys.length ? `<svg class="hc-poly" viewBox="0 0 1 1" preserveAspectRatio="none">${polys.join("")}</svg>` : "";
  return svg + boxes.join("");
}

function media(c, kind = "last", withBoxes = true, when = c.checked_at) {
  if (!when) return `<div class="hc-noimg">${c.status === "offline" ? "Offline in NX" : "No picture yet"}</div>`;
  return `<img src="${imgUrl(c, kind, when)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'hc-noimg',textContent:'No picture'}))">`
    + (withBoxes ? `<div class="hc-overlay">${overlay(c.issues)}</div>` : "");
}

function card(c) {
  const st = shown(c);
  const chips = (c.issues || []).map((i) => `<span class="chip hc-chip--${i.severity}">${esc(i.title)}</span>`).join("")
    || (c.status === "ok" ? `<span class="chip hc-chip--ok">${c.learning ? "OK · learning its reference" : "OK"}</span>` : "");
  return `<article class="hc hc--${st}" data-key="${esc(key(c))}" tabindex="0" role="button" aria-label="${esc(c.name)}: ${esc(STATUS[c.status] || c.status)}">
    <div class="hc-media">${media(c)}<span class="hc-state">${st === "muted" ? "Muted" : esc(STATUS[c.status] || c.status)}</span></div>
    <div class="hc-body">
      <div class="hc-name">${tenantTag(c)}${esc(c.name)}</div>
      <div class="muted hc-sub">${esc(c.site_name)} · ${c.checked_at ? `checked ${relTime(c.checked_at)}` : "not checked yet"}${c.mode === "night" ? " · night (IR)" : ""}</div>
      <div class="hc-issues">${chips}</div>
    </div></article>`;
}

function filtered() {
  const show = showSel.value, site = siteSel.value, text = q.value.trim().toLowerCase();
  return data.cameras.filter((c) => {
    if (site && String(c.site_id) !== site) return false;
    if (text && !c.name.toLowerCase().includes(text)) return false;
    const st = shown(c);
    if (show === "attention") return st === "problem" || st === "warning";
    if (show === "muted") return st === "muted";
    if (show === "offline") return st === "offline";
    return true;
  });
}

function render() {
  const s = data.summary || {};
  const part = (k, label, cls) => `<button class="chip hc-sum hc-chip--${cls}" data-show="${k === "ok" ? "all" : k === "problem" || k === "warning" ? "attention" : k}">${s[k] || 0} ${label}${(s[k] || 0) !== 1 && (k === "problem" || k === "warning") ? "s" : ""}</button>`;
  summaryEl.innerHTML = part("problem", "problem", "problem") + part("warning", "warning", "warning") + part("ok", "OK", "ok")
    + (s.offline ? part("offline", "offline", "offline") : "") + (s.muted ? part("muted", "muted", "offline") : "")
    + (s.pending ? `<span class="chip">${s.pending} not checked yet</span>` : "");
  const every = data.interval_s || 300;
  metaEl.textContent = `Every ${every < 60 ? `${every} s` : `${Math.round(every / 60)} min`} · ${data.ai ? "AI checks on (GPU)" : "AI checks off"}`
    + (data.last_check ? ` · last check ${relTime(data.last_check)}` : "");
  const list = filtered();
  grid.innerHTML = list.length ? list.map(card).join("")
    : `<div class="empty">${showSel.value === "attention" ? (data.cameras.length ? "Every camera looks fine." : "No cameras checked yet: the vision worker checks each camera every few minutes.") : "No cameras match."}</div>`;
}

async function load() {
  data = await api("/api/camera-health");
  const sites = [...new Map(data.cameras.map((c) => [c.site_id, c.site_name])).entries()].sort((a, b) => a[1].localeCompare(b[1]));
  const cur = siteSel.value || params.get("site") || "";
  siteSel.innerHTML = `<option value="">All sites</option>` + sites.map(([id, n]) => `<option value="${id}">${esc(n)}</option>`).join("");
  siteSel.value = sites.some(([id]) => String(id) === cur) ? cur : "";
  render();
  if (open && dlg.open) openCamera(open.site_id, open.device_id, false);
}

// ---- one camera ----
async function openCamera(siteId, deviceId, show = true) {
  open = { site_id: siteId, device_id: deviceId };
  const c = await api(`/api/camera-health/${siteId}/${encodeURIComponent(deviceId)}`);
  document.getElementById("hcd-title").textContent = c.name;
  document.getElementById("hcd-sub").innerHTML = `${esc(c.site_name)} · ${esc(STATUS[c.status] || c.status)}${c.muted ? ` · muted until ${esc(fmtTime(c.muted_until))}` : ""}`
    + ` · NX: ${esc(c.nx_status || "?")}${c.mode ? ` · ${c.mode === "night" ? "night (IR)" : "day"}` : ""}${c.ai ? " · AI checked" : ""}`;
  const refKind = `ref_${c.mode || "day"}`;
  const ref = c.refs?.[refKind];
  document.getElementById("hcd-ref").innerHTML = ref ? media(c, refKind, false, ref.taken_at) : `<div class="hc-noimg">No ${c.mode === "night" ? "night" : "day"} reference yet: it's learned from two checks in a row that agree</div>`;
  document.getElementById("hcd-ref-when").textContent = ref ? `${fmtTime(ref.taken_at)}${ref.source === "user" ? " (accepted by hand)" : " (learned)"}` : "";
  document.getElementById("hcd-now").innerHTML = media(c);
  document.getElementById("hcd-now-when").textContent = c.checked_at ? fmtTime(c.checked_at) : "";
  document.getElementById("hcd-now").classList.toggle("hc-hide-boxes", !document.getElementById("hcd-boxes").checked);
  document.getElementById("hcd-issues").innerHTML = (c.issues || []).length
    ? (c.issues).map((i) => `<div class="hcd-issue hcd-issue--${i.severity}"><b>${esc(i.title)}</b> <span class="muted">since ${esc(relTime(i.since_ms))}</span><div>${esc(i.detail || "")}</div></div>`).join("")
    : `<div class="hcd-issue hcd-issue--ok"><b>${c.status === "offline" ? "Offline in NX: not checked" : c.status === "pending" ? "Not checked yet" : "Nothing wrong found"}</b>${c.error ? `<div>${esc(c.error)}</div>` : ""}</div>`;
  const admin = CFG.isAdmin;
  const viewOn = c.settings.view_checks ?? c.metrics.view_checks ?? true;   // the worker's choice (off for PTZ)
  document.getElementById("hcd-actions").innerHTML =
    `<button class="btn" data-act="check"${c.check_requested ? " disabled" : ""}>${c.check_requested ? "Checking…" : "Check now"}</button>`
    + (admin ? `<button class="btn btn-primary" data-act="accept" title="Use the picture on the right as this camera's ${c.mode === "night" ? "night" : "day"} reference: moved / scene change / blur… start over">Accept current view</button>`
      + (c.muted ? `<button class="btn" data-act="mute" data-hours="0">Unmute</button>`
        : `<button class="btn" data-act="mute" data-hours="24">Mute 24 h</button><button class="btn" data-act="mute" data-hours="168">Mute 7 days</button>`) : "");
  document.getElementById("hcd-settings").innerHTML = admin ? `<div class="hcd-set">
      <label><input type="checkbox" data-set="skip" ${c.settings.skip ? "" : "checked"}> Check this camera</label>
      <label title="Off by default for PTZ cameras (they move on purpose)"><input type="checkbox" data-set="view_checks" ${viewOn ? "checked" : ""}> Check for moved / scene change</label>
    </div>` : "";
  document.getElementById("hcd-history").innerHTML = (c.events || []).map((e) => `<li><span class="muted">${esc(fmtTime(e.at))}</span> ${
    e.change === "raised" ? `<b class="hc-t--${e.severity}">${esc(e.title)}</b>` : e.change === "cleared" ? `${esc(e.title)} <span class="muted">cleared</span>`
    : e.change === "reference" ? `New ${esc(e.mode)} reference ${e.source === "user" ? `accepted by ${esc(e.by || "?")}` : "learned"}`
    : e.change === "muted" ? `Muted ${e.hours} h by ${esc(e.by || "?")}` : e.change === "unmuted" ? `Unmuted by ${esc(e.by || "?")}` : esc(e.change)}</li>`).join("")
    || `<li class="muted">Nothing yet</li>`;
  document.getElementById("hcd-metrics").textContent = JSON.stringify(c.metrics, null, 1);
  if (show && !dlg.open) dlg.showModal();
}

dlg.addEventListener("click", async (e) => {
  if (e.target === dlg || e.target.id === "hcd-close") { dlg.close(); return; }
  const b = e.target.closest("[data-act]");
  if (!b || !open) return;
  const base = `/api/camera-health/${open.site_id}/${encodeURIComponent(open.device_id)}`;
  b.disabled = true;
  try {
    if (b.dataset.act === "check") { await api(`${base}/check`, { method: "POST" }); b.textContent = "Checking…"; return; }
    if (b.dataset.act === "accept") {
      if (!confirm("Use the current picture as this camera's reference? Anything it shows now (a moved view, a change) becomes normal.")) { b.disabled = false; return; }
      await api(`${base}/accept`, { method: "POST" });
      toast("New reference saved: checking again");
    }
    if (b.dataset.act === "mute") await api(`${base}/mute`, { method: "POST", body: { hours: Number(b.dataset.hours) } });
    await load();
  } catch (err) {
    toast(esc(err.message || "Failed"), { kind: "alarm" });
    b.disabled = false;
  }
});
dlg.addEventListener("change", async (e) => {
  if (e.target.id === "hcd-boxes") { document.getElementById("hcd-now").classList.toggle("hc-hide-boxes", !e.target.checked); return; }
  const k = e.target.dataset.set;
  if (!k || !open) return;
  const body = k === "skip" ? { skip: !e.target.checked } : { view_checks: e.target.checked };
  try {
    await api(`/api/camera-health/${open.site_id}/${encodeURIComponent(open.device_id)}/settings`, { method: "PUT", body });
    await load();
  } catch (err) { toast(esc(err.message || "Failed"), { kind: "alarm" }); }
});
document.getElementById("hcd-close").addEventListener("click", () => dlg.close());
dlg.addEventListener("close", () => { open = null; });

grid.addEventListener("click", (e) => {
  const el = e.target.closest(".hc");
  if (!el) return;
  const [s, ...d] = el.dataset.key.split(":");
  openCamera(Number(s), d.join(":"));
});
grid.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches(".hc")) e.target.click(); });
summaryEl.addEventListener("click", (e) => {
  const b = e.target.closest("[data-show]");
  if (b) { showSel.value = b.dataset.show; render(); }
});
[showSel, siteSel].forEach((el) => el.addEventListener("change", render));
q.addEventListener("input", render);
if (params.get("show")) showSel.value = params.get("show");

on("camera.health", () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(load, 1000); });
on("reconnect", load);
setInterval(render, 30_000);           // "checked 3m ago"
await load();
if (params.get("site") && params.get("cam")) openCamera(Number(params.get("site")), params.get("cam"));

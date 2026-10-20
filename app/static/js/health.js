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
const shown = (c) => (c.settings?.skip ? "off" : c.muted && (c.status === "problem" || c.status === "warning") ? "muted" : c.status);
const statusText = (c) => (c.settings?.skip ? "Checks off" : STATUS[c.status] || c.status);

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
    <div class="hc-media">${media(c)}<span class="hc-state">${st === "muted" ? "Muted" : esc(statusText(c))}</span></div>
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
    if (show === "offline") return st === "offline" || st === "off";
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
  document.getElementById("hcd-sub").innerHTML = `${esc(c.site_name)} · ${esc(statusText(c))}${c.muted ? ` · muted until ${esc(fmtTime(c.muted_until))}` : ""}`
    + ` · NX: ${esc(c.nx_status || "?")}${c.mode ? ` · ${c.mode === "night" ? "night (IR)" : "day"}` : ""}${c.ai ? " · AI checked" : ""}`;
  const admin = CFG.isAdmin, pending = c.settings.retake;
  document.getElementById("hcd-refs").innerHTML = ["day", "night"].map((mode) => {
    const ref = c.refs?.[`ref_${mode}`], now = c.mode === mode;
    const label = mode === "day" ? "Day reference" : "Night reference";
    const waiting = pending?.mode === mode;
    return `<figure class="hcd-ref${now ? " hcd-ref--now" : ""}"><figcaption>${label}${now ? ' <span class="chip">in use now</span>' : ""}
        <span class="muted">${ref ? `${esc(fmtTime(ref.taken_at))} ${ref.source === "user" ? "(taken by hand)" : "(learned)"}` : ""}</span></figcaption>
      <div class="hc-media">${ref ? media(c, `ref_${mode}`, false, ref.taken_at)
        : `<div class="hc-noimg">${mode === "night" ? "Learned after dark" : "Learned in daylight"}: two checks in a row that agree, nobody in view</div>`}</div>
      ${admin ? `<div class="hcd-ref-actions">
        ${waiting ? `<span class="muted">Retake pending: ${now ? "next check" : mode === "night" ? "tonight's first check" : "the morning's first check"} with nobody in view</span>
          <button class="btn btn-sm" data-ref="${mode}" data-ref-act="cancel">Cancel</button>`
          : `<button class="btn btn-sm" data-ref="${mode}" data-ref-act="retake" title="${now ? "Take a fresh picture now as the reference" : `Take the next ${mode} picture as the reference`}">Retake${now ? " now" : ` at ${mode === "night" ? "night" : "day"}`}</button>`}
        ${ref ? `<button class="btn btn-sm" data-ref="${mode}" data-ref-act="relearn" title="Forget it: it's learned again automatically">Relearn</button>` : ""}
      </div>` : ""}</figure>`;
  }).join("");
  const lib = c.metrics?.library_slots;
  document.getElementById("hcd-learn").textContent = c.metrics?.twilight ? "Dusk / dawn: only the basic checks run until the light settles."
    : lib != null ? `Area checks compare with the same time of day: ${lib} of 48 half-hours learned${c.metrics.area_learning ? " (none yet for this time: area checks wait)" : ""}.` : "";
  document.getElementById("hcd-now").innerHTML = media(c);
  document.getElementById("hcd-now-when").textContent = c.checked_at ? fmtTime(c.checked_at) : "";
  document.getElementById("hcd-now").classList.toggle("hc-hide-boxes", !document.getElementById("hcd-boxes").checked);
  document.getElementById("hcd-issues").innerHTML = (c.issues || []).length
    ? (c.issues).map((i) => `<div class="hcd-issue hcd-issue--${i.severity}"><b>${esc(i.title)}</b> <span class="muted">since ${esc(relTime(i.since_ms))}</span><div>${esc(i.detail || "")}</div></div>`).join("")
    : `<div class="hcd-issue hcd-issue--ok"><b>${c.status === "offline" ? "Offline in NX: not checked" : c.status === "pending" ? "Not checked yet" : "Nothing wrong found"}</b>${c.error ? `<div>${esc(c.error)}</div>` : ""}</div>`;
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
  const rb = e.target.closest("[data-ref-act]");
  if (rb && open) {
    const act = rb.dataset.refAct, mode = rb.dataset.ref;
    if (act === "relearn" && !confirm(`Forget the ${mode} reference? It's learned again automatically (two checks in a row that agree).`)) return;
    rb.disabled = true;
    try {
      await api(`/api/camera-health/${open.site_id}/${encodeURIComponent(open.device_id)}/reference`, { method: "POST", body: { mode, action: act } });
      if (act === "retake") toast(`The ${mode} reference will be retaken on the next ${mode} check with nobody in view`);
      await load();
    } catch (err) { toast(esc(err.message || "Failed"), { kind: "alarm" }); rb.disabled = false; }
    return;
  }
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

// ---- TWG only: the vision worker's processing (GPU, models, queue, recent checks) ----
const hp = document.getElementById("hp");
const ms = (v) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);

// One series per chart (the caption names it): 2px line, ~10% area wash, end dot with a surface ring, hover readout.
function spark(el, title, pts, { max, unit, fmt = (v) => `${Math.round(v)}${unit}` }) {
  if (!pts.length) { el.innerHTML = `<figcaption>${esc(title)}</figcaption><div class="muted hp-empty">No samples yet</div>`; return; }
  const W = 320, H = 72, P = 4, t0 = pts[0].t, t1 = Math.max(pts[pts.length - 1].t, t0 + 1);
  const x = (t) => P + (t - t0) / (t1 - t0) * (W - 2 * P), y = (v) => H - P - Math.min(v, max) / max * (H - 2 * P);
  const line = pts.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  el.innerHTML = `<figcaption>${esc(title)} <b>${esc(fmt(last.v))}</b> <span class="muted">last 2 h</span></figcaption>
    <div class="hp-plot"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(title)}, now ${esc(fmt(last.v))}">
      <line class="hp-grid" x1="0" x2="${W}" y1="${y(max)}" y2="${y(max)}" /><line class="hp-grid" x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" />
      <polygon class="hp-area" points="${x(t0)},${y(0)} ${line} ${x(last.t)},${y(0)}" />
      <polyline class="hp-line" points="${line}" />
      <line class="hp-cross" x1="0" x2="0" y1="0" y2="${H}" hidden />
      <circle class="hp-dot" cx="${x(last.t)}" cy="${y(last.v)}" r="4" />
    </svg><div class="hp-tip" hidden></div></div>`;
  const svg = el.querySelector("svg"), cross = svg.querySelector(".hp-cross"), tip = el.querySelector(".hp-tip");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect(), tx = t0 + ((e.clientX - r.left) / r.width * W - P) / (W - 2 * P) * (t1 - t0);
    const p = pts.reduce((a, b) => (Math.abs(b.t - tx) < Math.abs(a.t - tx) ? b : a));
    const px = x(p.t) / W * r.width;
    cross.setAttribute("x1", x(p.t)); cross.setAttribute("x2", x(p.t)); cross.hidden = false;
    tip.hidden = false; tip.style.left = `${Math.min(Math.max(px, 40), r.width - 40)}px`;
    tip.textContent = `${new Date(p.t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} · ${fmt(p.v)}`;
  });
  svg.addEventListener("pointerleave", () => { cross.hidden = true; tip.hidden = true; });
}

async function loadProcessing() {
  if (!hp || !hp.open) return;
  const r = await api("/api/camera-health/processing").catch(() => null);
  const d = r?.data;
  document.getElementById("hp-age").textContent = !d ? "· the worker hasn't reported yet"
    : r.age_s > 120 ? `· last report ${Math.round(r.age_s / 60)} min ago: is the vision container running?` : `· updated ${r.age_s} s ago`;
  if (!d) return;
  const g = d.gpu || {};
  const tile = (label, value, sub = "", pct = null) => `<div class="hp-tile"><div class="muted">${esc(label)}</div><div class="hp-val">${esc(value)}</div>`
    + (pct != null ? `<div class="hp-bar" role="img" aria-label="${Math.round(pct)}%"><i style="width:${Math.min(100, pct)}%"></i></div>` : "")
    + `${sub ? `<div class="muted hp-sub">${esc(sub)}</div>` : ""}</div>`;
  document.getElementById("hp-tiles").innerHTML = [
    tile("GPU", g.name || (d.ai ? "?" : "Off (CPU only)"), g.driver ? `driver ${g.driver}` : g.error || ""),
    tile("GPU busy", g.util != null ? `${g.util}%` : "—", g.clock_mhz ? `${g.clock_mhz} MHz` : "", g.util),
    tile("GPU memory", g.mem_used_mb != null ? `${(g.mem_used_mb / 1024).toFixed(1)} of ${(g.mem_total_mb / 1024).toFixed(1)} GB` : "—", "", g.mem_total_mb ? g.mem_used_mb / g.mem_total_mb * 100 : null),
    tile("Temperature", g.temp_c != null ? `${g.temp_c} °C` : "—", [g.power_w != null ? `${g.power_w} W of ${g.power_limit_w ?? "?"} W` : "", g.fan != null ? `fan ${g.fan}%` : ""].filter(Boolean).join(" · ")),
    tile("Schedule", `every ${Math.round(d.interval_s / 60)} min`, `${d.parallel} camera at a time · ${d.checked_cameras} of ${d.cameras} cameras checked`),
    tile("Now", d.busy?.length ? `checking ${d.busy.join(", ")}` : `next in ${d.next_in_s} s`, `${d.due_now} waiting · avg check ${ms(d.avg_check_ms)}`),
    tile("Worker", `${d.process?.rss_mb ?? "?"} MB`, `CPU ${d.process?.cpu_s ?? "?"} s since ${fmtTime(d.started_at)}${d.errors_recent ? ` · ${d.errors_recent} recent errors` : ""}`),
  ].join("");
  const hist = d.gpu_history || [];
  spark(document.getElementById("hp-util"), "GPU busy", hist.map((p) => ({ t: p.t, v: p.util })), { max: 100, unit: "%" });
  spark(document.getElementById("hp-mem"), "GPU memory", hist.map((p) => ({ t: p.t, v: p.mem })),
    { max: g.mem_total_mb || Math.max(1, ...hist.map((p) => p.mem)), unit: "", fmt: (v) => `${(v / 1024).toFixed(2)} GB` });
  document.getElementById("hp-models").innerHTML = Object.values(d.models || {}).map((m) =>
    `<tr><td>${esc(m.model)}</td><td>${m.calls}</td><td>${ms(m.avg_ms)}</td><td>${ms(m.p95_ms)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">${d.ai ? "No model calls in the last hour" : "AI off: CPU checks only"}</td></tr>`;
  document.getElementById("hp-recent").innerHTML = (d.recent || []).slice(0, 12).map((c) => {
    const gp = Object.entries(c.gpu_ms || {}).map(([k, v]) => `${k} ${ms(v)}`).join(" · ");
    return `<tr><td class="hp-nw">${esc(new Date(c.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }))}</td>
      <td>${esc(c.camera)}</td><td class="hp-nw">${esc(c.how)} ${ms(c.fetch_ms)}</td><td class="hp-gpu">${esc(gp || "—")}</td><td class="hp-nw">${ms(c.total_ms)}</td>
      <td>${c.error ? `<span class="hc-t--problem">${esc(c.error)}</span>` : c.issues?.length ? `<span class="hc-t--${c.status}">${esc(c.issues.join(", "))}</span>` : "OK"}${c.library != null ? ` <span class="muted">· ${c.library} same-time frames</span>` : ""}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">No checks yet</td></tr>`;
}
if (hp) {
  hp.addEventListener("toggle", loadProcessing);
  setInterval(loadProcessing, 15_000);
  loadProcessing();
}

on("camera.health", () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(load, 1000); });
on("reconnect", load);
setInterval(render, 30_000);           // "checked 3m ago"
await load();
if (params.get("site") && params.get("cam")) openCamera(Number(params.get("site")), params.get("cam"));

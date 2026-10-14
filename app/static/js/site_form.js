import { api, esc, createMap, pinIcon, armChip, armLine } from "./common.js";

const site = window.SITE;
const $ = (id) => document.getElementById(id);
const fields = ["name", "host", "nx_user", "address", "lat", "lng", "notes"];
const result = $("conn-result");
let pickMap, pin;

if (site) {
  fields.forEach((f) => { if (site[f] !== null && site[f] !== undefined) $(f).value = site[f]; });
  $("host").value = site.cloud_id && site.host.includes(".relay.vmsproxy.com") ? site.cloud_id : site.host;
}
// The address is required for new sites. A site added before that may still be saved without one, but is asked for it.
if (site && !(site.address || "").trim()) {
  $("address").required = false;
  const warn = document.createElement("div");
  warn.className = "alert alert-warn small-print";
  warn.style.marginTop = "6px";
  warn.textContent = "This site has no address yet. Add it: dispatch texts, directions and incident reports use it.";
  $("address").closest(".field").appendChild(warn);
}

function payload(connect = true) {
  const num = (v) => (v === "" ? null : Number(v));
  return {
    name: $("name").value, host: $("host").value, nx_user: $("nx_user").value, nx_pass: $("nx_pass").value,
    address: $("address").value, lat: num($("lat").value), lng: num($("lng").value), notes: $("notes").value, connect,
    timezone: $("timezone").value, arm_schedule: schedule(),
  };
}

// ---------------------------------------------------------------- arming schedule
// Rows of "Arm/Disarm at <time> on <days>", a week preview of what they add up to (armed green, disarmed grey),
// and the card's own Save (schedule + time zone only; the NX connection isn't touched). A new site saves it
// with the main button.
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const DAY_SETS = { all: DAYS, weekdays: DAYS.slice(0, 5), weekend: DAYS.slice(5) };
const rowsEl = $("sched-rows");
const clock = (hhmm) => {                       // "18:00" -> "6:00 PM"
  const [h, m] = hhmm.split(":").map(Number);
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
};
const TIMES = Array.from({ length: 96 }, (_, i) => `${String(Math.floor(i / 4)).padStart(2, "0")}:${String((i % 4) * 15).padStart(2, "0")}`);
function timeOptions(selected) {
  const list = TIMES.includes(selected) ? TIMES : [...TIMES, selected].sort();
  return list.map((t) => `<option value="${t}" ${t === selected ? "selected" : ""}>${clock(t)}</option>`).join("");
}

function addEntry(e = { action: "arm", time: "18:00", days: DAYS.slice(0, 5) }, { quiet = false } = {}) {
  const row = document.createElement("div");
  row.className = "sched-row";
  row.innerHTML = `
    <div class="seg sched-action" role="group" aria-label="Arm or disarm">
      <button type="button" data-act="arm">Arm</button><button type="button" data-act="disarm">Disarm</button></div>
    <span class="muted">at</span>
    <select class="sched-time" aria-label="Time">${timeOptions(e.time)}</select>
    <span class="muted">on</span>
    <span class="sched-days">${DAYS.map((d) => `<label class="daybox"><input type="checkbox" value="${d}" ${e.days.includes(d) ? "checked" : ""}>${DAY_LABEL[d]}</label>`).join("")}</span>
    <span class="sched-quick">${Object.keys(DAY_SETS).map((k) => `<button type="button" class="link-btn" data-days="${k}">${{ all: "Every day", weekdays: "Weekdays", weekend: "Weekend" }[k]}</button>`).join("")}</span>
    <button type="button" class="btn btn-sm sched-del" aria-label="Remove this time" title="Remove">✕</button>`;
  setAction(row, e.action);
  rowsEl.appendChild(row);
  if (!quiet) changed();
}
function setAction(row, act) {
  row.dataset.action = act;
  row.querySelectorAll(".sched-action button").forEach((b) => b.classList.toggle("active", b.dataset.act === act));
}
function schedule() {
  return [...rowsEl.querySelectorAll(".sched-row")].map((r) => ({
    action: r.dataset.action, time: r.querySelector(".sched-time").value,
    days: [...r.querySelectorAll(".daybox input:checked")].map((c) => c.value),
  }));
}

// What the schedule adds up to: for every minute of the week, the latest entry at or before it (wrapping
// round from the end of the week), exactly like the server's arming.state_at.
function weekSpans(entries) {
  const ev = entries.flatMap((e) => e.days.map((d) => {
    const [h, m] = e.time.split(":").map(Number);
    return { at: DAYS.indexOf(d) * 1440 + h * 60 + m, armed: e.action === "arm" };
  })).sort((a, b) => a.at - b.at);
  if (!ev.length) return null;
  const spans = [];
  let state = ev[ev.length - 1].armed, from = 0;
  for (const x of ev) {
    if (x.armed !== state) { spans.push({ from, to: x.at, armed: state }); from = x.at; state = x.armed; }
  }
  spans.push({ from, to: 7 * 1440, armed: state });
  return spans;
}
function paintWeek(entries) {
  const box = $("sched-week"), spans = weekSpans(entries.filter((e) => e.days.length));
  box.hidden = !spans;
  if (!spans) return;
  const hhmm = (min) => `${String(Math.floor(min / 60) % 24).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;
  box.innerHTML = `<div class="sched-week-head"><span>What this schedule does</span>
      <span class="sched-legend"><i class="armed"></i>Armed <i class="disarmed"></i>Disarmed</span></div>
    ${DAYS.map((d, i) => {
      const lo = i * 1440, hi = lo + 1440;
      const parts = spans.filter((s) => s.to > lo && s.from < hi).map((s) => {
        const a = Math.max(s.from, lo) - lo, b = Math.min(s.to, hi) - lo;
        return `<span class="${s.armed ? "armed" : "disarmed"}" style="left:${(a / 14.4).toFixed(3)}%;width:${((b - a) / 14.4).toFixed(3)}%"
          title="${DAY_LABEL[d]} ${clock(hhmm(a))} – ${b === 1440 ? "midnight" : clock(hhmm(b))}: ${s.armed ? "armed" : "disarmed"}"></span>`;
      }).join("");
      return `<div class="sched-day"><b>${DAY_LABEL[d]}</b><div class="sched-bar">${parts}</div></div>`;
    }).join("")}
    <div class="sched-day sched-ticks"><b></b><div>${["12 AM", "6 AM", "12 PM", "6 PM", ""].map((t, i) => `<span style="left:${i * 25}%">${t}</span>`).join("")}</div></div>`;
}

// Problems the server would refuse, shown before saving.
function problems(entries) {
  const out = [], seen = new Map();
  entries.forEach((e, i) => {
    if (!e.days.length) out.push(`Row ${i + 1} (${e.action} at ${clock(e.time)}) has no days ticked.`);
    for (const d of e.days) {
      const k = `${d} ${e.time}`, other = seen.get(k);
      if (other && other !== e.action) out.push(`${DAY_LABEL[d]} ${clock(e.time)} is set to both arm and disarm.`);
      seen.set(k, e.action);
    }
  });
  return [...new Set(out)];
}

let savedKey = "";
const currentKey = () => JSON.stringify({ tz: $("timezone").value, s: schedule() });
function changed() {
  const entries = schedule(), bad = problems(entries);
  rowsEl.querySelector(".empty")?.remove();
  if (!entries.length) rowsEl.insertAdjacentHTML("beforeend", '<div class="empty muted">No schedule: the site stays in whatever state it was last set to by hand (armed by default).</div>');
  rowsEl.querySelectorAll(".sched-row").forEach((r, i) => r.classList.toggle("bad", !entries[i].days.length));
  $("sched-problems").innerHTML = bad.length ? `<div class="alert alert-error small-print">${bad.map(esc).join("<br>")}</div>` : "";
  paintWeek(entries);
  const dirty = currentKey() !== savedKey;
  $("sched-save").disabled = !site || !dirty || bad.length > 0;
  $("sched-undo").hidden = !site || !dirty;
  $("sched-state").innerHTML = !site ? "Saved with <b>Save &amp; connect</b> above." : dirty ? '<span class="unsaved">Unsaved changes</span>' : "Saved";
  $("sched-save-bar").classList.toggle("dirty", !!site && dirty);
}

rowsEl.addEventListener("click", (e) => {
  const row = e.target.closest(".sched-row");
  if (!row) return;
  if (e.target.closest(".sched-del")) { row.remove(); changed(); return; }
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act) { setAction(row, act); changed(); return; }
  const set = e.target.closest("[data-days]")?.dataset.days;
  if (set) { row.querySelectorAll(".daybox input").forEach((c) => { c.checked = DAY_SETS[set].includes(c.value); }); changed(); }
});
rowsEl.addEventListener("change", changed);
$("timezone").addEventListener("change", changed);
$("sched-add").addEventListener("click", () => addEntry());
$("sched-preset").addEventListener("click", () => {
  if (schedule().length && !confirm("Replace the times below with business hours (disarm 7:00 AM, arm 6:00 PM, Monday to Friday)?")) return;
  rowsEl.innerHTML = "";
  addEntry({ action: "disarm", time: "07:00", days: DAYS.slice(0, 5) }, { quiet: true });
  addEntry({ action: "arm", time: "18:00", days: DAYS.slice(0, 5) });
});

function loadSchedule(s) {
  rowsEl.innerHTML = "";
  (s?.arm_schedule || []).forEach((e) => addEntry(e, { quiet: true }));
  savedKey = currentKey();
  changed();
}
$("sched-undo").addEventListener("click", () => { $("timezone").value = site.timezone || ""; loadSchedule(site); });
$("sched-save").addEventListener("click", async () => {
  const btn = $("sched-save");
  btn.disabled = true;
  $("sched-state").textContent = "Saving…";
  try {
    const data = await api(`/api/sites/${site.id}/arm-schedule`, { method: "PUT", body: { timezone: $("timezone").value, arm_schedule: schedule() } });
    Object.assign(site, data);
    loadSchedule(site);
    $("arm-now").innerHTML = `Now: ${armChip(site)} ${armLine(site)}`;
    $("sched-state").innerHTML = '<span style="color:var(--ok)">Saved ✓</span> It takes over at the next scheduled time.';
  } catch (err) {
    $("sched-state").innerHTML = `<span class="text-failed">Not saved: ${esc(err.message)}</span>`;
    btn.disabled = false;
  }
});
window.addEventListener("beforeunload", (e) => { if (site && currentKey() !== savedKey && !leaving) { e.preventDefault(); e.returnValue = ""; } });
let leaving = false;

const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
const zones = (Intl.supportedValuesOf?.("timeZone") || [browserTz]).slice();
if (site?.timezone && !zones.includes(site.timezone)) zones.unshift(site.timezone);
$("timezone").innerHTML = `<option value="">Portal default${site ? ` (${esc(site.timezone_effective)})` : ""}</option>`
  + zones.map((z) => `<option value="${esc(z)}">${esc(z.replaceAll("_", " "))}</option>`).join("");
$("timezone").value = site ? site.timezone || "" : browserTz;
loadSchedule(site);
if (site) $("arm-now").innerHTML = `Now: ${armChip(site)} ${armLine(site)}`;

function showResult(kind, html) { result.innerHTML = `<div class="alert alert-${kind}">${html}</div>`; }

function okHtml(info) {
  return `<b>Connected.</b> NX site <b>${esc(info.nx_site_name || "—")}</b>, version ${esc(info.nx_version || "?")}, ${info.camera_count} camera(s).`;
}

$("test-btn").addEventListener("click", async () => {
  const btn = $("test-btn");
  btn.disabled = true; showResult("warn", "Connecting…");
  try {
    const p = payload();
    const info = await api("/api/sites/test", { method: "POST", body: { host: p.host, nx_user: p.nx_user, nx_pass: p.nx_pass, site_id: site?.id } });
    showResult("ok", okHtml(info));
    if (!$("name").value && info.nx_site_name) $("name").value = info.nx_site_name;
  } catch (e) {
    showResult("error", `<b>Connection failed:</b> ${esc(e.message)}`);
  } finally { btn.disabled = false; }
});

async function save(connect) {
  const btn = $("save-btn");
  btn.disabled = true;
  if (connect) showResult("warn", "Connecting…");
  try {
    await api(site ? `/api/sites/${site.id}` : "/api/sites", { method: site ? "PUT" : "POST", body: payload(connect) });
    leaving = true;
    location.href = "/sites";
  } catch (e) {
    const offerSkip = e.status === 400 && connect && e.detail?.status;
    showResult("error", `<b>${connect ? "Could not connect" : "Could not save"}:</b> ${esc(e.message)}
      ${offerSkip ? '<div style="margin-top:8px"><button type="button" class="btn btn-sm" id="save-anyway">Save anyway (poller keeps retrying)</button></div>' : ""}`);
    $("save-anyway")?.addEventListener("click", () => save(false));
    btn.disabled = false;
  }
}
$("site-form").addEventListener("submit", (e) => { e.preventDefault(); if (e.target.reportValidity()) save(true); });

// ---------------------------------------------------------------- location picker
function setPin(lat, lng, pan = true) {
  $("lat").value = lat.toFixed(6);
  $("lng").value = lng.toFixed(6);
  if (!pin) {
    pin = L.marker([lat, lng], { icon: pinIcon("ok"), draggable: true, title: "Site location" }).addTo(pickMap);
    pin.on("dragend", () => { const p = pin.getLatLng(); setPin(p.lat, p.lng, false); });
  } else {
    pin.setLatLng([lat, lng]);
  }
  if (pan) pickMap.setView([lat, lng], Math.max(pickMap.getZoom(), 17));
}

function initialView() {
  const lat = parseFloat($("lat").value), lng = parseFloat($("lng").value);
  return Number.isNaN(lat) || Number.isNaN(lng) ? null : [lat, lng];
}

const start = initialView();
pickMap = createMap($("pick-map"), { center: start || [39.8, -98.6], zoom: start ? 17 : 4 });
pickMap.on("click", (e) => setPin(e.latlng.lat, e.latlng.lng, false));
if (start) setPin(start[0], start[1], false);

const geoBox = document.createElement("div");
$("address").closest(".field").appendChild(geoBox);

async function findAddress() {
  const q = $("address").value.trim();
  if (q.length < 3) return;
  const btn = $("geocode-btn");
  btn.disabled = true;
  geoBox.innerHTML = '<small class="muted">Searching…</small>';
  try {
    const results = await api(`/api/geocode?q=${encodeURIComponent(q)}`);
    if (!results.length) { geoBox.innerHTML = '<small class="muted">No match. Try adding city and state, or click the map.</small>'; return; }
    const pick = (r) => { $("address").value = r.label; setPin(r.lat, r.lng); geoBox.innerHTML = ""; };
    if (results.length === 1) return pick(results[0]);
    geoBox.innerHTML = `<small class="muted">Pick the match:</small><div style="display:flex;flex-direction:column;gap:4px;margin-top:4px">${
      results.map((r, i) => `<button type="button" class="btn btn-sm" style="justify-content:flex-start;text-align:left;height:auto;padding:6px 10px" data-geo="${i}">${esc(r.label)}</button>`).join("")}</div>`;
    geoBox.querySelectorAll("[data-geo]").forEach((b) => b.addEventListener("click", () => pick(results[Number(b.dataset.geo)])));
  } catch (e) {
    geoBox.innerHTML = `<small class="muted">Address lookup failed (${esc(e.message)}). Click the map instead.</small>`;
  } finally {
    btn.disabled = false;
  }
}
$("geocode-btn").addEventListener("click", findAddress);
$("address").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); findAddress(); } });
["lat", "lng"].forEach((f) => $(f).addEventListener("change", () => {
  const v = initialView();
  if (v) setPin(v[0], v[1]);
}));

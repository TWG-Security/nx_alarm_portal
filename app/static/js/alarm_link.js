// The page a texted alarm link opens (/a/<token>): one alarm, its clip and live video, and live views of
// the site's other cameras, no sign-in. Views live in the address hash so the phone's Back button works:
//   #clip (default)  the alarm clip          #live         the alarm camera, live
//   #cameras         every camera, as stills  #cam/<id>     one camera, live
import { CFG, fmtTime } from "./common.js";
import { mountPlayer } from "./player.js";
import { mountLive } from "./live.js";
import { createSharer, isIOS, locationPermission } from "./share_core.js";
import { mountLocationTest } from "./location_test.js";

const a = CFG.alarm, token = CFG.token;
const STILL_EVERY_MS = 3000;     // a still per visible camera every 3 s (~40 KB each): light on phone data and the NX relay
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// "Share my location now": the person this was texted to shows on the dispatcher's map while this page is open.
if (CFG.canShare && $("link-share-start")) {
  const KEY = `twg-share-from-${token}`;
  let sharer = null;
  const paint = (kind, text, facts) => {
    const on = !["off", "gone", "blocked"].includes(kind);
    if (kind === "blocked") document.querySelector(".share-help").open = true;
    $("link-share-start").hidden = on; $("link-share-stop").hidden = !on;
    $("link-share-state").textContent = kind === "off" && text === "Not sharing" ? "Not sharing your location." : text;
    $("link-share-state").className = `share-line share-line--${kind}`;
    $("link-share-facts").textContent = facts;
    try { if (on) sessionStorage.setItem(KEY, "1"); else sessionStorage.removeItem(KEY); } catch { /* fine */ }
  };
  // The location request happens right here in the tap (Safari asks "Allow location?" only then); the location
  // link's token is fetched alongside and used for the first position.
  function startSharing() {
    if (!sharer) {
      sharer = createSharer(async () => {
        const r = await fetch(`/api/a/${token}/share`, { method: "POST", credentials: "omit" });
        if (!r.ok) throw new Error(r.status === 410 ? "This alarm link has expired" : "Couldn't start sharing");
        return (await r.json()).token;
      }, { everyS: 10, onState: paint });
    }
    sharer.start();
  }
  $("link-share-start").addEventListener("click", startSharing);

  // En route / On scene / Clear, like an MDT: dispatch sees each tap live, with the time and where they were.
  // En route also starts sharing the location (so dispatch can watch them come).
  const WORDS = { en_route: "En route", on_scene: "On scene", clear: "Clear" };
  const showStatus = (st, note = "") => {
    document.querySelectorAll(".mdt-btn").forEach((b) => b.classList.toggle("on", b.dataset.status === st));
    $("mdt-state").textContent = st ? `Your status: ${WORDS[st]}${note}` : "Tap your status so dispatch knows where you are.";
  };
  showStatus(CFG.status || "");
  document.querySelector(".mdt").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-status]");
    if (!b) return;
    const st = b.dataset.status;
    document.querySelectorAll(".mdt-btn").forEach((x) => { x.disabled = true; });
    try {
      if (st === "en_route" && !sharer?.active) startSharing();
      const r = await fetch(`/api/a/${token}/status`, { method: "POST", credentials: "omit",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: st }) });
      if (!r.ok) throw new Error(r.status === 410 ? "This alarm link has expired" : "Couldn't reach dispatch. Try again.");
      showStatus(st, ` · sent to dispatch ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`);
    } catch (err) { $("mdt-state").textContent = err.message; }
    finally { document.querySelectorAll(".mdt-btn").forEach((x) => { x.disabled = false; }); }
  });
  $("link-share-stop").addEventListener("click", () => sharer?.stop());

  // Notes to dispatch: each goes on the alarm's notes and timeline at once (dispatch hears a chime on an active call).
  const notes = [...(CFG.notes || [])];
  const paintNotes = () => {
    $("link-notes-list").innerHTML = notes.map((n) => `<li><span class="muted small-print">${esc(fmtTime(n.at))}</span><div>${esc(n.text)}</div></li>`).join("");
  };
  paintNotes();
  async function sendNote() {
    const box = $("link-note-text"), text = box.value.trim(), btn = $("link-note-send");
    if (!text) { box.focus(); return; }
    btn.disabled = true; $("link-note-state").textContent = "Sending…";
    try {
      const r = await fetch(`/api/a/${token}/note`, { method: "POST", credentials: "omit",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      if (!r.ok) {
        let why = r.status === 410 ? "This alarm link has expired" : "Couldn't reach dispatch. Try again.";
        try { const d = await r.json(); if (r.status === 429 && d.detail) why = d.detail; } catch { /* keep */ }
        throw new Error(why);
      }
      notes.push(await r.json()); paintNotes();
      box.value = "";
      $("link-note-state").textContent = `Sent to dispatch ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    } catch (err) { $("link-note-state").textContent = err.message; }   // the text stays in the box to try again
    finally { btn.disabled = false; }
  }
  $("link-note-send").addEventListener("click", sendNote);
  $("link-note-text").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendNote(); } });
  if (isIOS()) document.querySelector(".ios-tip").hidden = false;
  mountLocationTest(`/api/a/${token}/diag`);
  (async () => {
    let again = false;
    try { again = sessionStorage.getItem(KEY) === "1"; } catch { /* fine */ }
    if (again && await locationPermission() === "granted") startSharing();         // no tap: only if already allowed
  })();
}

$("link-when").textContent = new Date(a.event_ts_ms).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
$("link-expires").textContent = fmtTime($("link-expires").dataset.iso);

const alarmCard = $("link-alarm"), camsCard = $("link-cameras"), player = $("link-player"), body = $("link-cams-body");
let current = null;              // the mounted player / live view / stills grid: destroyed on every view change
let cameras = null;              // the site's cameras, fetched once

function stop() { current?.destroy(); current = null; }

function parse() {
  const h = location.hash.replace(/^#/, "");
  if (h.startsWith("cam/")) return { view: "cam", id: decodeURIComponent(h.slice(4)) };
  if (h === "live" || h === "cameras") return { view: h };
  return { view: a.device_id ? "clip" : "info" };
}

function tabs(view) {
  for (const b of document.querySelectorAll(".link-view")) {
    const on = b.dataset.view === view;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  }
}

async function loadCameras() {
  if (cameras) return cameras;
  const r = await fetch(`/api/a/${token}/cameras`, { cache: "no-store" });
  if (!r.ok) throw new Error(r.status === 410 ? "This alarm link has expired." : "Couldn't load the cameras.");
  cameras = (await r.json()).cameras;
  return cameras;
}

// Stills that refresh while they're on screen and the page is visible; tap one for live video.
function mountGrid(root, list) {
  root.innerHTML = `<div class="cam-grid">${list.map((c) => `
    <button class="cam-tile${c.alarm ? " cam-tile--alarm" : ""}" type="button" data-cam="${esc(c.device_id)}">
      <span class="cam-shot"><img alt="" decoding="async"><span class="cam-wait muted">Loading…</span></span>
      <span class="cam-name">${c.alarm ? '<span class="chip p2">Alarm camera</span> ' : ""}${esc(c.name)}</span>
    </button>`).join("")}</div>`;
  let destroyed = false;
  const tiles = [...root.querySelectorAll(".cam-tile")].map((el) => ({ el, img: el.querySelector("img"), wait: el.querySelector(".cam-wait"),
    id: el.dataset.cam, seen: false, busy: false, timer: null }));

  function load(t) {
    if (destroyed || t.busy || !t.seen || document.visibilityState === "hidden") return;
    t.busy = true;
    const next = new Image();
    next.onload = () => { t.img.src = next.src; t.wait.hidden = true; t.busy = false; t.timer = setTimeout(() => load(t), STILL_EVERY_MS); };
    next.onerror = () => { t.wait.hidden = false; t.wait.textContent = "Camera not reachable"; t.busy = false; t.timer = setTimeout(() => load(t), STILL_EVERY_MS * 2); };
    next.src = `/media/a/${token}/cameras/${encodeURIComponent(t.id)}/live.jpg?t=${Date.now()}`;
  }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const t = tiles.find((x) => x.el === e.target);
      t.seen = e.isIntersecting;
      if (t.seen) load(t); else clearTimeout(t.timer);
    }
  }, { rootMargin: "100px" });
  tiles.forEach((t) => io.observe(t.el));
  const onVis = () => { if (document.visibilityState === "visible") tiles.forEach(load); };
  document.addEventListener("visibilitychange", onVis);
  root.addEventListener("click", (e) => {
    const tile = e.target.closest("[data-cam]");
    if (tile) location.hash = `cam/${encodeURIComponent(tile.dataset.cam)}`;
  });
  return { destroy() { destroyed = true; io.disconnect(); tiles.forEach((t) => clearTimeout(t.timer));
    document.removeEventListener("visibilitychange", onVis); root.innerHTML = ""; } };
}

async function show() {
  const { view, id } = parse();
  stop();
  const onAlarm = view === "clip" || view === "live" || view === "info";
  alarmCard.hidden = !onAlarm;
  camsCard.hidden = onAlarm;
  tabs(view === "cam" ? "cameras" : view);
  if (view === "clip") {
    current = mountPlayer(player, a, { apiBase: `/api/a/${token}`, mediaBase: `/media/a/${token}`, liveToggle: false });
  } else if (view === "live") {
    current = mountLive(player, a, { base: `/media/a/${token}` });
  } else if (!onAlarm) {
    window.scrollTo(0, 0);
    const back = camsCard.querySelector('[data-go="cameras"]');
    back.hidden = view !== "cam";
    $("link-cams-note").hidden = view === "cam";
    $("link-cams-title").textContent = view === "cam" ? "Camera" : "All cameras";
    body.innerHTML = '<p class="muted">Loading the cameras…</p>';
    let list;
    try { list = await loadCameras(); } catch (e) { body.innerHTML = `<p class="alert alert-error">${esc(e.message)}</p>`; return; }
    if (parse().view !== view || parse().id !== id) return;           // moved on while loading
    if (view === "cameras") {
      $("link-cams-title").textContent = `All cameras${CFG.alarm.site_name ? ` · ${CFG.alarm.site_name}` : ""}`;
      if (!list.length) { body.innerHTML = '<p class="muted">No cameras found for this site.</p>'; return; }
      current = mountGrid(body, list);
    } else {
      const cam = list.find((c) => c.device_id === id);
      if (!cam) { body.innerHTML = '<p class="muted">That camera isn\'t on this site.</p>'; return; }
      $("link-cams-title").textContent = cam.name;
      current = mountLive(body, { device_id: cam.device_id }, { base: `/media/a/${token}/cameras/${encodeURIComponent(cam.device_id)}` });
    }
  }
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-view], [data-go]");
  if (!b) return;
  const to = b.dataset.view || b.dataset.go, was = location.hash;
  location.hash = to === "clip" ? "" : to;
  if (location.hash === was) show();                     // same hash: no hashchange fires
});
window.addEventListener("hashchange", show);
if (!a.device_id) document.querySelectorAll('.link-view[data-view="clip"], .link-view[data-view="live"]').forEach((b) => { b.hidden = true; });
show();

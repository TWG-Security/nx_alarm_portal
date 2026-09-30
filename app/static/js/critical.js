// Full-screen pop-up for critical alarms, on every page. Oldest unhandled first.

import { openAlarms, on, emit, esc, fmtTime, timerHtml, acknowledge } from "./common.js";
import { silence, isSilenced } from "./sound.js";

const dlg = document.getElementById("critical-dialog");
const MIN_KEY = "twg-crit-minimized";
let minimized = new Set();
try { minimized = new Set(JSON.parse(sessionStorage.getItem(MIN_KEY) || "[]")); } catch (e) { /* ignore */ }
let shownId = null, idx = 0, signature = "";

const criticals = () => [...openAlarms.values()].filter((a) => a.priority === 1).sort((a, b) => a.event_ts_ms - b.event_ts_ms);
const queue = () => criticals().filter((a) => !minimized.has(a.id));
function saveMin() { try { sessionStorage.setItem(MIN_KEY, JSON.stringify([...minimized])); } catch (e) { /* ignore */ } }

function snapshotHtml(a) {
  if (!a.has_snapshot) return "";
  return `<div class="snapshot crit-snap"><img alt="Camera frame at alarm time" data-alarm="${a.id}" src="/media/alarms/${a.id}/snapshot.jpg">
    <div class="snap-msg" hidden></div></div>`;
}

// Event-time frames can lag the alarm by a few seconds; retry, then fall back to live.
function wireSnapshot(root) {
  const img = root.querySelector("img[data-alarm]");
  if (!img) return;
  let tries = 0;
  img.addEventListener("error", () => {
    tries++;
    const id = img.dataset.alarm;
    if (tries === 1) setTimeout(() => { img.src = `/media/alarms/${id}/snapshot.jpg?r=1`; }, 3000);
    else if (tries === 2) img.src = `/media/alarms/${id}/live.jpg?t=${Date.now()}`;
    else {
      img.classList.add("failed");
      const m = root.querySelector(".snap-msg");
      m.hidden = false;
      m.textContent = "No camera image available right now.";
    }
  });
}

function render() {
  const q = queue();
  if (!q.length) { if (dlg.open) dlg.close("empty"); shownId = null; signature = ""; return; }
  idx = Math.min(idx, q.length - 1);
  const a = q[idx];
  const sig = `${a.id}|${q.length}|${idx}`;
  if (sig === signature && dlg.open) return;          // nothing visible changed; keep typed note + image
  const keepNote = shownId === a.id ? dlg.querySelector("#crit-note")?.value : "";
  signature = sig;
  shownId = a.id;
  dlg.innerHTML = `
    <div class="crit-head">
      <div class="crit-flag"><span class="crit-dot"></span> CRITICAL ALARM</div>
      <div class="crit-timer">Active ${timerHtml(a.event_ts_ms)}</div>
    </div>
    <div class="crit-body">
      <h1 id="crit-title">${esc(a.caption)}</h1>
      <div class="crit-where"><b>${esc(a.site_name)}</b>${a.site_address ? ` · ${esc(a.site_address)}` : ""}</div>
      <div class="muted">${a.source_name ? esc(a.source_name) + " · " : ""}${fmtTime(a.event_ts_ms)}</div>
      ${a.description ? `<p style="white-space:pre-wrap">${esc(a.description)}</p>` : ""}
      ${snapshotHtml(a)}
      <div class="field"><label for="crit-note">Disposition note</label>
        <textarea id="crit-note" maxlength="4000" placeholder="What did you see / do? (e.g. verified on camera, dispatched, false alarm)"></textarea></div>
      <div class="crit-actions">
        <button class="btn btn-primary" data-act="ack">Acknowledge</button>
        <button class="btn" data-act="map">Show on map</button>
        <button class="btn" data-act="silence" ${isSilenced() ? "disabled" : ""}>Silence 2 min</button>
        <button class="btn" data-act="min" title="Hide this pop-up. Alarms stay open in the feed">Minimize</button>
      </div>
      ${q.length > 1 ? `<div class="crit-pager"><button class="btn btn-sm" data-act="prev" ${idx === 0 ? "disabled" : ""}>‹ Prev</button>
        <span>${idx + 1} of ${q.length} critical</span>
        <button class="btn btn-sm" data-act="next" ${idx === q.length - 1 ? "disabled" : ""}>Next ›</button></div>` : ""}
    </div>`;
  dlg.querySelector("#crit-note").value = keepNote || "";
  wireSnapshot(dlg);
  if (!dlg.open) dlg.showModal();
}

dlg.addEventListener("click", async (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!act) return;
  const a = queue()[idx];
  if (!a) return render();
  if (act === "ack") {
    await acknowledge(a.id, dlg.querySelector("#crit-note").value, e.target);   // store update re-renders
  } else if (act === "map") {
    minimized.add(a.id); saveMin(); render();
    if (location.pathname === "/") emit("focus-alarm", a); else location.href = `/?alarm=${a.id}`;
  } else if (act === "silence") {
    silence(); e.target.disabled = true;
  } else if (act === "min") {
    queue().forEach((x) => minimized.add(x.id)); saveMin(); render();
  } else if (act === "prev" || act === "next") {
    idx += act === "next" ? 1 : -1; render();
  }
});
// Esc = minimize, never a silent dismiss.
dlg.addEventListener("cancel", (e) => { e.preventDefault(); queue().forEach((x) => minimized.add(x.id)); saveMin(); render(); });

on("store", render);
on("alarm.arrived", (a) => { if (a.priority === 1) { idx = Math.max(0, queue().findIndex((x) => x.id === a.id)); render(); } });
export function showCriticals() { minimized.clear(); saveMin(); render(); }

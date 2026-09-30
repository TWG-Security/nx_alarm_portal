// Alarm detail drawer: event-time frame, live view, details, acknowledge. Used on every page.

import { api, esc, fmtTime, relTime, timerHtml, acknowledge, on, toast, PRIORITY } from "./common.js";

const drawer = document.getElementById("drawer");
const backdrop = document.getElementById("drawer-backdrop");
let current = null, liveTimer = null;

export function closeDrawer() {
  current = null;
  clearInterval(liveTimer);
  drawer.hidden = true;
  backdrop.hidden = true;
}

function render(a) {
  const acked = a.state === "acknowledged";
  const nx = a.nx_ack_result;
  const keepNote = drawer.querySelector("#drawer-note")?.value || "";
  drawer.innerHTML = `
    <div class="drawer-head">
      <div><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span> ${acked ? "" : timerHtml(a.event_ts_ms)}
        <h1 style="margin-top:8px">${esc(a.caption)}</h1>
        <div><b>${esc(a.site_name)}</b>${a.site_address ? ` <span class="muted">· ${esc(a.site_address)}</span>` : ""}</div>
        ${a.source_name ? `<div class="muted">${esc(a.source_name)}</div>` : ""}</div>
      <button class="btn btn-sm" id="drawer-close" aria-label="Close">✕</button>
    </div>
    ${a.has_snapshot ? `
      <div class="snapshot"><img id="snap" alt="Camera frame at alarm time" src="/media/alarms/${a.id}/snapshot.jpg">
        <div class="snap-msg" id="snap-msg" hidden></div></div>
      <div class="seg" role="group" aria-label="Frame" style="margin-bottom:6px">
        <button type="button" class="active" data-frame="event">At alarm time</button>
        <button type="button" data-frame="live">Live</button>
      </div>` : '<p class="muted">This event has no camera attached.</p>'}
    <dl class="kv">
      <dt>Event time</dt><dd>${fmtTime(a.event_ts_ms)}</dd>
      <dt>Received</dt><dd>${fmtTime(a.received_at)}</dd>
      <dt>Type</dt><dd>${esc(a.event_type)}${a.event_subtype ? ` <span class="muted mono">${esc(a.event_subtype)}</span>` : ""}</dd>
      ${a.description ? `<dt>Description</dt><dd style="white-space:pre-wrap">${esc(a.description)}</dd>` : ""}
      <dt>In NX</dt><dd>${a.nx_ack_required ? "Acknowledging here also clears it in NX" : "A bookmark is added in NX on acknowledge"}</dd>
    </dl>
    <div class="ack-box">
      ${acked ? `
        <h2>Acknowledged</h2>
        <dl class="kv">
          <dt>By</dt><dd>${esc(a.acked_by || "—")}</dd>
          <dt>At</dt><dd>${fmtTime(a.acked_at)} <span class="muted">(${relTime(a.acked_at)})</span></dd>
          <dt>Note</dt><dd style="white-space:pre-wrap">${esc(a.ack_note || "—")}</dd>
          <dt>NX write-back</dt><dd>${nx ? (nx.ok ? `<span class="chip acked">${esc(nx.method)}</span>` : `<span class="chip offline">failed</span> ${esc(nx.error || "")}`) : "—"}</dd>
        </dl>` : `
        <div class="field"><label for="drawer-note">Disposition note</label>
          <textarea id="drawer-note" maxlength="4000" placeholder="What did you see / do?"></textarea></div>
        <button class="btn btn-primary" id="drawer-ack">Acknowledge</button>`}
      <a class="btn btn-sm" style="margin-left:8px" href="/audit?alarm_id=${a.id}">Audit trail</a>
    </div>`;
  const note = drawer.querySelector("#drawer-note");
  if (note) note.value = keepNote;
  const img = drawer.querySelector("#snap");
  if (img) {
    img.addEventListener("error", () => {
      img.classList.add("failed");
      const m = drawer.querySelector("#snap-msg");
      m.hidden = false;
      m.textContent = "No frame available (archive not recorded at that time, or the site is unreachable).";
    });
    img.addEventListener("load", () => { img.classList.remove("failed"); drawer.querySelector("#snap-msg").hidden = true; });
  }
}

export async function openDrawer(id, fallback) {
  let a = fallback;
  try { a = await api(`/api/alarms/${id}`); } catch (e) { if (!a) { toast(esc(e.message), { kind: "error" }); return; } }
  current = a;
  clearInterval(liveTimer);
  drawer.innerHTML = "";
  render(a);
  drawer.hidden = false;
  backdrop.hidden = false;
  drawer.querySelector("#drawer-close").focus();
}

drawer.addEventListener("click", async (e) => {
  if (e.target.id === "drawer-close") return closeDrawer();
  if (e.target.id === "drawer-ack") {
    const updated = await acknowledge(current.id, drawer.querySelector("#drawer-note").value, e.target);
    if (updated) { current = updated; render(updated); }
    return;
  }
  const frame = e.target.dataset.frame;
  if (frame) {
    e.target.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === e.target));
    const img = drawer.querySelector("#snap");
    clearInterval(liveTimer);
    if (frame === "live") {
      const tick = () => { img.src = `/media/alarms/${current.id}/live.jpg?t=${Date.now()}`; };
      tick();
      liveTimer = setInterval(tick, 2000);
    } else {
      img.src = `/media/alarms/${current.id}/snapshot.jpg`;
    }
  }
});
backdrop.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && current && !document.querySelector("dialog[open]")) closeDrawer(); });

// Someone else acknowledged the alarm we're looking at.
on("store", ({ alarm } = {}) => { if (current && alarm && alarm.id === current.id && alarm.state !== current.state) { current = alarm; render(alarm); } });

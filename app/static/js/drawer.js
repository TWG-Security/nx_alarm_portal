// Alarm detail drawer: event-time frame, live view, details, acknowledge. Used on every page.

import { api, esc, fmtTime, relTime, timerHtml, acknowledge, on, toast, PRIORITY } from "./common.js";
import { mountPlayer } from "./player.js";

const LEVEL_SOURCE = {
  rule_tag: "set by a #tag on the NX rule", force_ack: "NX rule forces acknowledgement",
  site: "site override", tenant: "Settings → Alarm levels", default: "default for this event type",
};
const drawer = document.getElementById("drawer");
const backdrop = document.getElementById("drawer-backdrop");
let current = null, player = null;

export function closeDrawer() {
  current = null;
  player?.destroy(); player = null;
  drawer.hidden = true;
  backdrop.hidden = true;
}

function render(a) {
  const acked = a.state === "acknowledged";
  const nx = a.nx_ack_result;
  const keepNote = drawer.querySelector("#drawer-note")?.value || "";
  drawer.querySelector(".drawer-top").innerHTML = `
    <div class="drawer-head">
      <div><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span> ${acked ? "" : timerHtml(a.event_ts_ms)}
        <h1 style="margin-top:8px">${esc(a.caption)}</h1>
        <div><b>${esc(a.site_name)}</b>${a.site_address ? ` <span class="muted">· ${esc(a.site_address)}</span>` : ""}</div>
        ${a.source_name ? `<div class="muted">${esc(a.source_name)}</div>` : ""}</div>
      <button class="btn btn-sm" id="drawer-close" aria-label="Close">✕</button>
    </div>`;
  drawer.querySelector(".drawer-details").innerHTML = `
    <dl class="kv">
      <dt>Event time</dt><dd>${fmtTime(a.event_ts_ms)}</dd>
      <dt>Received</dt><dd>${fmtTime(a.received_at)}</dd>
      <dt>Type</dt><dd>${esc(a.event_type)}${a.event_subtype ? ` <span class="muted mono">${esc(a.event_subtype)}</span>` : ""}</dd>
      ${a.description ? `<dt>Description</dt><dd style="white-space:pre-wrap">${esc(a.description)}</dd>` : ""}
      <dt>Level</dt><dd>${PRIORITY[a.priority]} <span class="muted">· ${esc(LEVEL_SOURCE[a.level_source] || "event type")}</span></dd>
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
}

export async function openDrawer(id, fallback) {
  let a = fallback;
  try { a = await api(`/api/alarms/${id}`); } catch (e) { if (!a) { toast(esc(e.message), { kind: "error" }); return; } }
  current = a;
  player?.destroy();
  drawer.innerHTML = '<div class="drawer-top"></div><div class="player-mount"></div><div class="drawer-details"></div>';
  render(a);
  player = mountPlayer(drawer.querySelector(".player-mount"), a);
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
});
backdrop.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && current && !document.querySelector("dialog[open]")) closeDrawer(); });

// Someone else acknowledged the alarm we're looking at.
on("store", ({ alarm } = {}) => { if (current && alarm && alarm.id === current.id && alarm.state !== current.state) { current = alarm; render(alarm); } });

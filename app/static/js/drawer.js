// Alarm detail drawer: the recorded clip and live video side by side, details, acknowledge with a
// verdict (real event / false alarm). Used on every page.

import { CFG, api, esc, fmtTime, relTime, timerHtml, acknowledge, applyAlarm, on, toast, PRIORITY, VERDICT, verdictChip,
         can } from "./common.js";
import { mountLive } from "./live.js";
import { mountPlayer } from "./player.js";

const LEVEL_SOURCE = {
  rule_tag: "set by a #tag on the NX rule", force_ack: "NX rule forces acknowledgement",
  site: "site override", tenant: "Settings → Alarm levels", default: "default for this event type",
};
const drawer = document.getElementById("drawer");
const backdrop = document.getElementById("drawer-backdrop");
let current = null, player = null, live = null;

export function closeDrawer() {
  current = null;
  player?.destroy(); player = null;
  live?.destroy(); live = null;
  drawer.hidden = true;
  backdrop.hidden = true;
}

function render(a) {
  const acked = a.state === "acknowledged";
  const disarmed = a.state === "disarmed";
  const nx = a.nx_ack_result;
  const override = can("alarms.bulk_edit");
  const verdictRow = `<dt>Verdict</dt><dd>${verdictChip(a) || '<span class="muted">Not marked</span>'}
    ${a.verdict_by ? `<span class="muted">by ${esc(a.verdict_by)} · ${relTime(a.verdict_at)}</span>` : ""}
    ${override ? `<div class="verdict-override">${Object.entries(VERDICT).filter(([v]) => v !== a.verdict).map(([v, label]) =>
      `<button class="btn btn-sm" data-override="${v}" title="Admin override (logged)">Mark ${label.toLowerCase()}</button>`).join("")}</div>` : ""}</dd>`;
  const keepNote = drawer.querySelector("#drawer-note")?.value || "";
  drawer.querySelector(".drawer-top").innerHTML = `
    <div class="drawer-head">
      <div><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span> ${disarmed ? '<span class="chip disarmed">Site disarmed</span>' : acked ? "" : timerHtml(a.event_ts_ms)}
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
      ${disarmed ? `
        <h2>Received while the site was disarmed</h2>
        <p class="muted">This event was recorded but not raised: no sound, no pop-up, no live feed card. Nothing to acknowledge.
          To have an NX rule raise alarms even while its site is disarmed, add <b>#24h</b> to the rule's Title/Comment.</p>
        <dl class="kv">${verdictRow}</dl>` : acked ? `
        <h2>Acknowledged</h2>
        <dl class="kv">
          <dt>By</dt><dd>${esc(a.acked_by || "—")}</dd>
          <dt>At</dt><dd>${fmtTime(a.acked_at)} <span class="muted">(${relTime(a.acked_at)})</span></dd>
          ${verdictRow}
          <dt>Note</dt><dd style="white-space:pre-wrap">${esc(a.ack_note || "—")}</dd>
          <dt>NX write-back</dt><dd>${nx ? (nx.ok ? `<span class="chip acked">${esc(nx.method)}</span>` : `<span class="chip offline">failed</span> ${esc(nx.error || "")}`) : "—"}</dd>
        </dl>` : `
        <div class="field"><label for="drawer-note">Disposition note</label>
          <textarea id="drawer-note" maxlength="4000" placeholder="What did you see / do?"></textarea></div>
        <div class="verdict-buttons"><span class="muted ack-as">Acknowledge as</span>
          <button class="btn btn-verdict-real" data-ack="real">Real event</button>
          <button class="btn btn-verdict-false" data-ack="false">False alarm</button></div>`}
      <a class="btn btn-sm" style="margin-left:8px" href="/audit?alarm_id=${a.id}">Audit trail</a>
      <button class="btn btn-sm" id="drawer-export" title="Incident report PDF, or PDF + video clip + stills">Export report / clip…</button>
    </div>`;
  const note = drawer.querySelector("#drawer-note");
  if (note) note.value = keepNote;
}

export async function openDrawer(id, fallback) {
  let a = fallback;
  try { a = await api(`/api/alarms/${id}`); } catch (e) { if (!a) { toast(esc(e.message), { kind: "error" }); return; } }
  current = a;
  player?.destroy(); live?.destroy(); live = null;
  drawer.innerHTML = `<div class="drawer-top"></div>
    <div class="drawer-media${a.device_id ? "" : " single"}">
      <section><div class="media-label">Recorded <span class="muted">around the alarm</span></div><div class="player-mount"></div></section>
      ${a.device_id ? '<section><div class="media-label">Live <span class="muted">now</span></div><div class="live-mount"></div></section>' : ""}
    </div>
    <div class="drawer-details"></div>`;
  render(a);
  player = mountPlayer(drawer.querySelector(".player-mount"), a, { liveToggle: false });
  if (a.device_id) live = mountLive(drawer.querySelector(".live-mount"), a);
  drawer.hidden = false;
  backdrop.hidden = false;
  drawer.querySelector("#drawer-close").focus();
}

drawer.addEventListener("click", async (e) => {
  if (e.target.id === "drawer-close") return closeDrawer();
  if (e.target.id === "drawer-export") return openExport(current);
  const verdict = e.target.dataset.ack;
  if (verdict) {
    const updated = await acknowledge(current.id, drawer.querySelector("#drawer-note").value, e.target, verdict);
    if (updated) { current = updated; render(updated); }
    return;
  }
  const override = e.target.dataset.override;
  if (override) {
    const why = prompt(`Change the verdict to “${VERDICT[override]}”? This admin override is logged.\nReason (optional):`, "");
    if (why === null) return;
    e.target.disabled = true;
    try {
      await api("/api/alarms/bulk", { method: "POST", body: { ids: [current.id], verdict: override, note: why } });
      const updated = await api(`/api/alarms/${current.id}`);
      current = updated; applyAlarm(updated); render(updated);
      toast(`Verdict changed to ${VERDICT[override].toLowerCase()}`);
    } catch (err) { toast(esc(err.message), { kind: "error" }); e.target.disabled = false; }
  }
});
backdrop.addEventListener("click", closeDrawer);

// ---------------------------------------------------------------- export (app/services/report.py)
const exp = document.getElementById("export-dialog");
const expStatus = document.getElementById("export-status");
let exporting = null;

function openExport(a) {
  exporting = a;
  const c = CFG.clip || { pre: 10, post: 20 };
  const windows = [[c.pre, c.post], [30, 60], [60, 120]].filter((w, i, all) => all.findIndex((x) => x[0] === w[0] && x[1] === w[1]) === i);
  document.getElementById("export-window").innerHTML = windows.map(([p, q], i) =>
    `<option value="${p},${q}">${p} s before to ${q >= 60 ? `${q / 60} min` : `${q} s`} after${i === 0 ? " (default)" : ""}</option>`).join("");
  document.getElementById("export-summary").textContent = `${a.caption} · ${a.site_name}${a.source_name ? " · " + a.source_name : ""} · ${fmtTime(a.event_ts_ms)}`;
  const noVideo = !a.device_id;
  exp.querySelector('input[value="zip"]').disabled = noVideo;
  if (noVideo) exp.querySelector('input[value="pdf"]').checked = true;
  expStatus.innerHTML = noVideo ? '<div class="alert alert-warn">This alarm has no camera, so the report has no video or screenshots.</div>' : "";
  exp.showModal();
}

document.getElementById("export-cancel").addEventListener("click", () => exp.close());
document.getElementById("export-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!exporting) return;
  const btn = document.getElementById("export-submit");
  const format = exp.querySelector('input[name="export-format"]:checked').value;
  const [pre, post] = document.getElementById("export-window").value.split(",");
  const quality = document.getElementById("export-quality").value;
  const q = new URLSearchParams({ format, pre, post, quality, note: document.getElementById("export-note").value });
  btn.disabled = true;
  const t0 = Date.now();
  expStatus.innerHTML = `<div class="alert alert-warn">Preparing the ${format === "zip" ? "evidence package" : "report"}…
    ${quality === "hd" ? "HD clips take up to a minute." : "Usually a few seconds; longer if the clip is still recording."}</div>`;
  try {
    const res = await fetch(`/api/alarms/${exporting.id}/export?${q}`, { credentials: "same-origin" });
    if (!res.ok) {
      const d = res.headers.get("content-type")?.includes("json") ? (await res.json()).detail : `HTTP ${res.status}`;
      throw new Error(typeof d === "string" ? d : JSON.stringify(d));
    }
    const blob = await res.blob();
    const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "")?.[1] || `alarm-${exporting.id}.${format}`;
    const url = URL.createObjectURL(blob);
    const link = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    expStatus.innerHTML = `<div class="alert alert-ok">Downloaded <b>${esc(name)}</b> (${(blob.size / 1_048_576).toFixed(1)} MB, ${((Date.now() - t0) / 1000).toFixed(1)} s). The export is in the audit log.</div>`;
  } catch (err) {
    expStatus.innerHTML = `<div class="alert alert-error"><b>Export failed:</b> ${esc(err.message)}</div>`;
  } finally { btn.disabled = false; }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && current && !document.querySelector("dialog[open]")) closeDrawer(); });

// Someone else acknowledged the alarm we're looking at.
on("store", ({ alarm } = {}) => { if (current && alarm && alarm.id === current.id && alarm.state !== current.state) { current = alarm; render(alarm); } });

// Alarm detail drawer: the recorded clip and live video side by side, details, acknowledge with a
// verdict (real event / false alarm / dismissed), the closest guards with Dispatch, and Next (the next open
// alarm in the feed's order: most urgent first, oldest first). Used on every page.

import { CFG, api, esc, fmtTime, relTime, timerHtml, acknowledge, applyAlarm, on, toast, PRIORITY, VERDICT, verdictChip, can, isMine, createMap, openAlarms, inView, alarmQueue } from "./common.js";
import { freshness } from "./guards.js";
import { mountLive } from "./live.js";
import { mountPlayer } from "./player.js";

const LEVEL_SOURCE = {
  rule_tag: "set by a #tag on the NX rule", force_ack: "NX rule forces acknowledgement",
  site: "site override", tenant: "Settings → Alarm levels", default: "default for this event type",
};
const drawer = document.getElementById("drawer");
const backdrop = document.getElementById("drawer-backdrop");
let current = null, player = null, live = null, notes = [];
let where = null;    // { map, plan, site }: the top-down map of where the alarm's camera is
let guardsTimer = null, guardsFor = null, lastGuards = null;     // lastGuards: { alarmId, r } from /guards

// ---------------------------------------------------------------- Next: the next open alarm in working order
function nextAlarm(a) {
  const q = alarmQueue();
  const i = q.findIndex((x) => x.id === a.id);
  const rest = q.filter((x) => x.id !== a.id);
  if (!rest.length) return null;
  return { alarm: i >= 0 && i + 1 < q.length ? q[i + 1] : rest[0], count: rest.length };
}
function nextHtml(a) {
  const n = nextAlarm(a);
  return n ? `<button class="btn btn-sm btn-primary" id="drawer-next" data-next="${n.alarm.id}"
      title="${esc(`${PRIORITY[n.alarm.priority]}: ${n.alarm.caption} at ${n.alarm.site_name}`)}">Next alarm › <span class="next-count">${n.count} more open</span></button>` : "";
}
function paintNext() {
  const slot = drawer.querySelector(".drawer-next-slot");
  if (slot && current) slot.innerHTML = nextHtml(current);
}
on("store", () => { if (current && !drawer.hidden) paintNext(); });

// ---------------------------------------------------------------- guards: closest first, Dispatch
function stopGuards() { clearInterval(guardsTimer); guardsTimer = null; guardsFor = null; }
// The two closest guards that can be texted, as big Dispatch buttons right under the title (phones see them first).
function quickHtml(a) {
  const g = lastGuards?.alarmId === a.id ? lastGuards.r : null;
  if (!g || !g.texts_ready || !(isMine(a) || can("platform.support"))) return "";
  const pick = g.guards.filter((c) => c.can_text).slice(0, 2);
  if (!pick.length) return "";
  const sent = new Set(g.dispatched.map((d) => d.to));
  return `<div class="dispatch-quick">${pick.map((c, i) => `<button type="button" class="btn dispatch-big ${i === 0 ? "btn-primary" : ""}"
      data-dispatch="${c.tracker_id}" data-name="${esc(c.name)}">
      <span class="dispatch-big-main">Dispatch ${esc(c.name)}</span>
      <span class="dispatch-big-sub">${c.distance_m == null ? "position unknown" : `${esc(c.distance)} away`}${c.closest ? " · closest" : ""}${!c.usable && c.distance_m != null ? " · old position" : ""}${sent.has(c.person) || sent.has(c.name) ? " · already sent" : ""}</span>
    </button>`).join("")}</div>`;
}
function paintQuick() {
  const slot = drawer.querySelector(".dispatch-quick-slot");
  if (slot && current) slot.innerHTML = quickHtml(current);
}
// Everyone sent to this alarm, like an MDT board: text delivered → opened → en route → on scene → clear.
const STATUS_WORD = { en_route: "En route", on_scene: "On scene", clear: "Clear" };
const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
function boardHtml(list) {
  if (!list.length) return "";
  return `<div class="mdt-board">${list.map((d) => {
    const word = STATUS_WORD[d.status] || (d.opened_at ? "Opened the link"
      : d.text === "delivered" ? "Text delivered" : ["failed", "undelivered"].includes(d.text) ? "Text NOT delivered" : "Dispatched");
    const cls = STATUS_WORD[d.status] ? d.status : ["failed", "undelivered"].includes(d.text) ? "failed" : "dispatched";
    return `<div class="mdt-row"><div><b>${esc(d.guard)}</b>${d.to && d.to !== d.guard ? ` <span class="muted">(${esc(d.to)})</span>` : ""}
        <span class="chip mdt-chip mdt-chip--${cls}">${esc(word)}</span></div>
      <div class="muted small-print">Sent ${esc(clock(d.at))}${d.auto ? " automatically" : d.by ? ` by ${esc(d.by)}` : ""}${d.distance ? `, ${esc(d.distance)} away` : ""}
        · text ${esc(d.text || "sending")}${d.opened_at ? ` · opened ${esc(clock(d.opened_at))}` : " · not opened yet"}${d.status_at ? ` · ${esc(STATUS_WORD[d.status])} ${esc(clock(d.status_at))}` : ""}</div></div>`;
  }).join("")}</div>`;
}
let chosen = new Set(), chosenFor = null;       // guards ticked for "Dispatch selected"
async function loadGuards(a) {
  const box = drawer.querySelector(".drawer-dispatch");
  if (!box) return;
  let r;
  try { r = await api(`/api/alarms/${a.id}/guards`); } catch { return; }
  if (current?.id !== a.id || guardsFor !== a.id) return;
  lastGuards = { alarmId: a.id, r };
  paintQuick();
  if (!r.guards.length && !r.dispatched.length) { box.hidden = true; return; }
  if (chosenFor !== a.id) { chosen = new Set(); chosenFor = a.id; }
  box.hidden = false;
  const write = isMine(a) || can("platform.support");
  const ok = r.guards.filter((c) => r.texts_ready && c.can_text);
  for (const id of [...chosen]) if (!ok.some((c) => c.tracker_id === id)) chosen.delete(id);
  const all = ok.length > 0 && ok.every((c) => chosen.has(c.tracker_id));
  box.innerHTML = `${r.dispatched.length ? `<div class="media-label">Dispatched <span class="muted">live status from their phones</span></div>${boardHtml(r.dispatched)}` : ""}
    <div class="media-label">Guards <span class="muted">closest to the ${r.from === "camera" ? "alarm camera" : "site"} first</span></div>
    ${write && ok.length > 1 ? `<div class="dispatch-choose"><label class="check"><input type="checkbox" data-choose-all ${all ? "checked" : ""}> All guards (${ok.length})</label>
      <button type="button" class="btn btn-sm btn-primary" data-dispatch-chosen ${chosen.size ? "" : "disabled"}>Dispatch selected (${chosen.size})</button></div>` : ""}
    <ul class="dispatch-list">${r.guards.map((c) => {
      const f = freshness({ fix_at: c.fix_at, online: c.online });
      const where = c.distance_m == null ? "position unknown" : `${c.distance} away`;
      const block = !r.texts_ready ? "Text messages aren't set up" : c.reason;
      return `<li class="${c.closest ? "closest" : ""}">${write && ok.length > 1 ? `<input type="checkbox" data-choose="${c.tracker_id}" aria-label="Choose ${esc(c.name)}" ${chosen.has(c.tracker_id) ? "checked" : ""} ${block ? "disabled" : ""}>` : ""}
        <span class="guard-pip guard--${f}"></span>
        <span class="dispatch-who"><b>${esc(c.name)}</b>${c.closest ? ' <span class="chip p2">Closest</span>' : ""}
          <span class="muted small-print">${esc(where)}${f === "offline" ? " · OFFLINE" : f === "stale" ? ` · last seen ${esc(relTime(c.fix_at))}` : ""}${c.person && c.person !== c.name ? ` · ${esc(c.person)}` : ""}</span>
          ${block ? `<span class="muted small-print">${esc(block)}</span>` : ""}</span>
        ${write ? `<button type="button" class="btn btn-sm ${c.closest ? "btn-primary" : ""}" data-dispatch="${c.tracker_id}" data-name="${esc(c.name)}" ${block ? "disabled" : ""}>Dispatch</button>` : ""}</li>`;
    }).join("")}</ul>
    ${r.auto && a.state === "new" && r.texts_ready ? '<p class="muted small-print">Marking it <b>Real event</b> texts the closest guard automatically.</p>' : ""}`;
}

// ---------------------------------------------------------------- timeline: everything, live (also in the report)
let timelineSoon = null;
async function loadTimeline(a) {
  const box = drawer.querySelector(".drawer-timeline");
  if (!box) return;
  let rows;
  try { rows = await api(`/api/alarms/${a.id}/timeline`); } catch { return; }
  if (current?.id !== a.id) return;
  box.hidden = false;
  box.innerHTML = `<div class="media-label">Timeline <span class="muted">newest first · live · ${rows.length} entries · also in the incident report</span></div>
    <ol class="timeline">${[...rows].reverse().map((e) => `<li class="tl tl--${esc(e.kind)}"><time>${esc(new Date(e.ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }))}</time>
      <span><b>${esc(e.what)}</b> <span class="muted">· ${esc(e.who)}</span>${e.detail ? `<div class="muted small-print">${esc(e.detail)}</div>` : ""}</span></li>`).join("")}</ol>`;
}
on("alarm.timeline", ({ alarm_id }) => {
  if (!current || current.id !== alarm_id || drawer.hidden) return;
  clearTimeout(timelineSoon);
  timelineSoon = setTimeout(async () => {
    if (current?.id !== alarm_id) return;
    loadTimeline(current); loadGuards(current);
    try {
      const a = await api(`/api/alarms/${alarm_id}`);
      if (current?.id === alarm_id && (a.call_state !== current.call_state || a.state !== current.state)) {
        const keep = drawer.querySelector("#call-note")?.value || "";
        current = a; render(a);
        if (keep && drawer.querySelector("#call-note")) drawer.querySelector("#call-note").value = keep;
      }
    } catch { /* the next event or the 15 s refresh catches up */ }
  }, 250);
});
function startGuards(a) {
  stopGuards();
  guardsFor = a.id;
  loadGuards(a);
  guardsTimer = setInterval(() => { if (current && !drawer.hidden) loadGuards(current); }, 15000);
}
async function sendDispatch(ids, names, btn) {
  if (!confirm(`Text ${names} to go to this alarm now?`)) return;
  btn.disabled = true;
  try {
    const r = await api(`/api/alarms/${current.id}/dispatch`, { method: "POST", body: { tracker_ids: ids } });
    toast(`Dispatched ${r.sent.map((x) => `${esc(x.to)}${x.distance ? ` (${esc(x.distance)})` : ""}`).join(", ")}`
      + (r.failed.length ? `. Not sent: ${r.failed.map((f) => `${esc(f.name)}: ${esc(f.error)}`).join("; ")}` : ""), r.failed.length ? { kind: "error" } : {});
    chosen.clear();
    loadGuards(current); loadTimeline(current);
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
  finally { btn.disabled = false; }
}
const dispatchGuard = (btn) => sendDispatch([Number(btn.dataset.dispatch)], btn.dataset.name, btn);

export function closeDrawer() {
  current = null;
  stopGuards();
  player?.destroy(); player = null;
  live?.destroy(); live = null;
  closeWhere();
  drawer.hidden = true;
  backdrop.hidden = true;
}

// A call: a guard was dispatched. A Real event stays open (Active calls) until End call.
function callHtml(a) {
  if (a.call_state === "active" && a.state !== "new") {
    return `<div class="call-box"><h2>Call in progress</h2>
      <p class="muted">A guard was sent. This alarm stays on the map's <b>Active calls</b> until you end the call.
        Ending it texts the guards who haven't tapped Clear.</p>
      <div class="field"><label for="call-note">How it ended <span class="muted">(optional, on the report)</span></label>
        <textarea id="call-note" maxlength="4000" rows="2" placeholder="e.g. Police took over; site secured; keyholder on site"></textarea></div>
      <button type="button" class="btn btn-primary call-end" id="call-end">End call</button></div>`;
  }
  if (a.call_state === "active" && a.state === "new") {
    return '<p class="call-hint">A guard is dispatched. <b>Real event</b> keeps this open as an active call until you end it; <b>False alarm</b> or <b>Dismiss</b> ends it and tells the guard to stand down.</p>';
  }
  if (a.call_state === "ended") {
    return `<p class="muted">Call ended${a.call_ended_by ? ` by ${esc(a.call_ended_by)}` : ""}${a.call_ended_at ? ` · ${esc(fmtTime(a.call_ended_at))}` : ""}${a.call_end_note ? `: “${esc(a.call_end_note)}”` : ""}</p>`;
  }
  return "";
}
async function endCall(btn) {
  if (!confirm("End this call? The guards still on it get a CALL ENDED text.")) return;
  btn.disabled = true;
  try {
    const a = await api(`/api/alarms/${current.id}/end-call`, { method: "POST", body: { note: drawer.querySelector("#call-note")?.value || "" } });
    current = a; applyAlarm(a); render(a); loadTimeline(a); loadGuards(a);
    toast("Call ended");
  } catch (err) { toast(esc(err.message), { kind: "error" }); btn.disabled = false; }
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
  const keepFollow = drawer.querySelector("#note-text")?.value || "";
  drawer.querySelector(".drawer-top").innerHTML = `
    <div class="drawer-head">
      <div><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span> ${disarmed ? '<span class="chip disarmed">Site disarmed</span>' : acked ? "" : timerHtml(a.event_ts_ms)}
        ${a.call_state === "active" ? ' <span class="chip call-chip">Call in progress</span>' : a.call_state === "ended" ? ' <span class="chip">Call ended</span>' : ""}
        <h1 style="margin-top:8px">${esc(a.caption)}</h1>
        ${isMine(a) ? "" : `<div><span class="chip tenant-chip">${esc(a.tenant_name || "Another company")}</span></div>`}
        <div><b>${esc(a.site_name)}</b>${a.site_address ? ` <span class="muted">· ${esc(a.site_address)}</span>` : ""}</div>
        ${a.source_name ? `<div class="muted">${esc(a.source_name)}</div>` : ""}</div>
      <div class="drawer-head-actions"><span class="drawer-next-slot">${nextHtml(a)}</span>
        <button class="btn btn-sm drawer-back" id="drawer-close" aria-label="Close">← ${location.pathname === "/" ? "Back to map" : "Back"}</button></div>
    </div>
    <div class="dispatch-quick-slot">${quickHtml(a)}</div>`;
  drawer.querySelector(".drawer-details").innerHTML = `
    <dl class="kv">
      <dt>Event time</dt><dd>${fmtTime(a.event_ts_ms)}</dd>
      <dt>Received</dt><dd>${fmtTime(a.received_at)}</dd>
      <dt>Type</dt><dd>${esc(a.event_type)}${a.event_subtype ? ` <span class="muted mono">${esc(a.event_subtype)}</span>` : ""}</dd>
      ${a.description ? `<dt>Description</dt><dd style="white-space:pre-wrap">${esc(a.description)}</dd>` : ""}
      <dt>Level</dt><dd>${PRIORITY[a.priority]} <span class="muted">· ${esc(LEVEL_SOURCE[a.level_source] || "event type")}</span></dd>
      <dt>In NX</dt><dd>${a.nx_ack_required ? "Acknowledging here also clears it in NX" : "A bookmark is added in NX on acknowledge (not when dismissed)"}</dd>
    </dl>
    <div class="ack-box">
      ${callHtml(a)}
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
          <dt>Disposition</dt><dd style="white-space:pre-wrap">${esc(a.ack_note || "—")}</dd>
          <dt>NX write-back</dt><dd>${nx ? (nx.ok ? `<span class="chip acked">${esc(nx.method)}</span>` : `<span class="chip offline">failed</span> ${esc(nx.error || "")}`) : "—"}</dd>
        </dl>` : `
        <div class="field"><label for="drawer-note">Disposition note <span class="muted">(saved when you acknowledge)</span></label>
          <textarea id="drawer-note" maxlength="4000" placeholder="What did you see / do?"></textarea></div>
        <div class="verdict-buttons"><span class="muted ack-as">Acknowledge as</span>
          <button class="btn btn-verdict-real" data-ack="real">Real event</button>
          <button class="btn btn-verdict-false" data-ack="false">False alarm</button>
          <button class="btn btn-verdict-dismissed" data-ack="dismissed" title="Close it without a verdict or an NX bookmark">Dismiss</button></div>`}
      <div class="notes-box">
        <h3>Notes</h3>
        <ul class="notes-list"></ul>
        <div class="note-add">
          <textarea id="note-text" maxlength="4000" rows="2" placeholder="Add a follow-up note (e.g. police on scene, keyholder called back). Ctrl+Enter to add."></textarea>
          <button class="btn" id="note-add" type="button">Add note</button>
        </div>
      </div>
      <a class="btn btn-sm" href="/audit?alarm_id=${a.id}">Audit trail</a>
      <button class="btn btn-sm" id="drawer-export" title="Incident report PDF, or PDF + video clip + stills">Export report / clip…</button>
    </div>`;
  const note = drawer.querySelector("#drawer-note");
  if (note) note.value = keepNote;
  drawer.querySelector("#note-text").value = keepFollow;
  renderNotes();
}

// ---------------------------------------------------------------- where: the site from straight above
// The alarm's camera is drawn red and flashing, other placed cameras orange (red if they have an open
// alarm too). Shown when the site has a location or the camera is placed; it's only a picture, so
// any failure just leaves it hidden.
async function mountWhere(a) {
  const section = drawer.querySelector(".drawer-where");
  if (!section) return;
  const site = (await api("/api/sites")).find((s) => s.id === a.site_id);
  if (current?.id !== a.id || !site) return;
  const placed = !!(a.device_id && site.camera_places?.[a.device_id]);
  if (site.lat == null && !placed) return;
  const { openPlan } = await import("./plan.js");
  if (current?.id !== a.id) return;
  section.hidden = false;
  const map = createMap(section.querySelector(".where-mount"), { scrollWheelZoom: false, center: [site.lat ?? 0, site.lng ?? 0], zoom: 18 });
  const cameras = () => Object.entries(where?.site.camera_places || {}).map(([deviceId, p]) => ({
    siteId: site.id, deviceId, ...p,
    alarm: deviceId === a.device_id || [...openAlarms.values()].some((x) => x.site_id === site.id && x.device_id === deviceId && inView(x)),
  }));
  where = { map, site, plan: null };
  where.plan = openPlan(map, { cameras });
  where.plan.focusCamera(site, a.device_id);
  const name = site.camera_places?.[a.device_id]?.name || a.source_name || "This camera";
  section.querySelector(".where-note").innerHTML = placed ? `<b>${esc(name)}</b> is the red cone.`
    : a.device_id ? `${esc(name)} isn't on the map yet.${CFG.isAdmin ? " Place it from Map → Top-down view → Place cameras." : " An admin can place it from the Map page."}`
    : "This alarm isn't from a camera.";
}
function closeWhere() {
  try { where?.plan?.destroy(); where?.map.remove(); } catch (err) { console.error(err); }
  where = null;
}
on("store", () => where?.plan?.update());
on("site.updated", (s) => { if (where && s.id === where.site.id) { where.site = { ...where.site, ...s }; where.plan.update(); } });

// ---------------------------------------------------------------- follow-up notes (append-only)
function renderNotes() {
  const ul = drawer.querySelector(".notes-list");
  if (!ul) return;
  ul.innerHTML = notes.length ? notes.map((n) => `
    <li><div class="note-meta"><b>${esc(n.by)}</b> · ${fmtTime(n.at)} <span class="muted">(${relTime(n.at)})</span></div>
      <div class="note-text">${esc(n.text)}</div></li>`).join("")
    : '<li class="muted note-empty">No follow-up notes yet.</li>';
}

async function addNote() {
  const box = drawer.querySelector("#note-text"), btn = drawer.querySelector("#note-add");
  const text = box.value.trim();
  if (!text || !current) return;
  btn.disabled = true;
  try {
    const n = await api(`/api/alarms/${current.id}/notes`, { method: "POST", body: { text } });
    if (!notes.some((x) => x.id === n.id)) notes.push(n);
    box.value = "";
    renderNotes();
  } catch (e) { toast(esc(e.message), { kind: "error" }); }
  finally { btn.disabled = false; }
}

export async function openDrawer(id, fallback) {
  let a = fallback;
  try { a = await api(`/api/alarms/${id}`); } catch (e) { if (!a) { toast(esc(e.message), { kind: "error" }); return; } }
  current = a;
  notes = [];
  player?.destroy(); live?.destroy(); live = null;
  closeWhere();
  drawer.innerHTML = `<div class="drawer-top"></div>
    <div class="drawer-media${a.device_id ? "" : " single"}">
      <section><div class="media-label">Recorded <span class="muted">around the alarm</span></div><div class="player-mount"></div></section>
      ${a.device_id ? '<section><div class="media-label">Live <span class="muted">now</span></div><div class="live-mount"></div></section>' : ""}
    </div>
    <div class="drawer-lower">
      <div class="drawer-details"></div>
      <div class="drawer-side"><section class="drawer-dispatch" hidden></section>
      <section class="drawer-where" hidden><div class="media-label">Where <span class="muted">top-down</span></div>
        <div class="where-mount"></div><div class="where-note muted"></div></section>
      <section class="drawer-timeline" hidden></section></div>
    </div>`;
  render(a);
  player = mountPlayer(drawer.querySelector(".player-mount"), a, { liveToggle: false });
  if (a.device_id) live = mountLive(drawer.querySelector(".live-mount"), a);
  mountWhere(a).catch((err) => console.error("top-down map in the alarm details failed", err));
  api(`/api/alarms/${a.id}/notes`).then((rows) => { if (current?.id === a.id) { notes = rows; renderNotes(); } }).catch(() => {});
  startGuards(a);
  loadTimeline(a);
  drawer.hidden = false;
  backdrop.hidden = false;
  drawer.querySelector("#drawer-close").focus();
}

drawer.addEventListener("click", async (e) => {
  if (e.target.closest("#drawer-close")) return closeDrawer();
  if (e.target.id === "call-end") return endCall(e.target);
  const next = e.target.closest("[data-next]");
  if (next) { const a = openAlarms.get(Number(next.dataset.next)); return openDrawer(Number(next.dataset.next), a); }
  const disp = e.target.closest("[data-dispatch]");
  if (disp) return dispatchGuard(disp);
  const many = e.target.closest("[data-dispatch-chosen]");
  if (many && chosen.size) {
    const names = lastGuards.r.guards.filter((c) => chosen.has(c.tracker_id)).map((c) => c.name);
    return sendDispatch([...chosen], names.length > 2 ? `${names.length} guards (${names.join(", ")})` : names.join(" and "), many);
  }
  if (e.target.id === "drawer-export") return openExport(current);
  if (e.target.id === "note-add") return addNote();
  const verdict = e.target.dataset.ack;
  if (verdict) {
    const updated = await acknowledge(current.id, drawer.querySelector("#drawer-note").value, e.target, verdict);
    if (updated) { current = updated; render(updated); loadGuards(updated); loadTimeline(updated); }
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
drawer.addEventListener("change", (e) => {
  if (!lastGuards || lastGuards.alarmId !== current?.id) return;
  if (e.target.matches("[data-choose]")) {
    const id = Number(e.target.dataset.choose);
    if (e.target.checked) chosen.add(id); else chosen.delete(id);
  } else if (e.target.matches("[data-choose-all]")) {
    chosen = new Set(e.target.checked ? lastGuards.r.guards.filter((c) => c.can_text).map((c) => c.tracker_id) : []);
  } else return;
  loadGuards(current);
});
backdrop.addEventListener("click", closeDrawer);
drawer.addEventListener("keydown", (e) => {
  if (e.target.id === "note-text" && e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); addNote(); }
});
// Someone else added a note to the alarm we're looking at.
on("alarm.note", (n) => {
  if (current && n.alarm_id === current.id && !notes.some((x) => x.id === n.id)) { notes.push(n); renderNotes(); }
  if (current && n.alarm_id === current.id && /^Dispatched /.test(n.text)) setTimeout(() => current && loadGuards(current), 1500);
});

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
on("store", ({ alarm, alarms } = {}) => {
  const a = current && (alarms || (alarm ? [alarm] : [])).findLast((x) => x.id === current.id);
  if (a && a.state !== current.state) { current = a; render(a); }
});

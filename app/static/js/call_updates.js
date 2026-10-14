// Guard updates on an active call (the user, 2026-10-14: "when there is an active call and the timeline is updated ...
// if the guard says en route it makes a noise to let the operator know, a different sound than we already use, and it
// shows on the right hand side and notifies"). The server marks what a guard did from their alarm link
// (app/audit.py GUARD_ACTIONS: opened the link, looked at the cameras, shared location, En route / On scene / Clear,
// a note) with "alarm.timeline" {guard: true}. For an active call this plays the "update" chime once (your own
// company's calls only, one tab), shows one toast at the bottom right listing every new update (click: open the
// call), and emits "call.update" so the map's Active calls card shows the guard's status and newest update.
import { api, on, emit, esc, toast, isMine } from "./common.js";
import { play } from "./sound.js";

const RECENT_MS = 60_000;        // an update older than this when first seen isn't news (screen opened later, clock skew)
const announced = new Map();     // alarm id -> Set of row keys already announced
const waiting = new Map();       // alarm id -> debounce timer (En route also starts location sharing: two rows at once)
const key = (r) => `${r.ts}|${r.what}|${r.who}|${r.detail}`;
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });

/** The guards' updates on an alarm: {status: newest En route / On scene / Clear row, last: newest guard row, rows}. */
export async function guardUpdates(alarmId) {
  const rows = (await api(`/api/alarms/${alarmId}/timeline`)).filter((r) => r.kind === "guard").sort((x, y) => x.ts - y.ts);
  return { status: rows.filter((r) => r.status).pop() || null, last: rows[rows.length - 1] || null, rows };
}

async function check(alarmId) {
  let a, u;
  try { [a, u] = await Promise.all([api(`/api/alarms/${alarmId}`), guardUpdates(alarmId)]); } catch { return; }
  if (a.call_state !== "active") return;
  const seen = announced.get(alarmId) || new Set();
  announced.set(alarmId, seen);
  const fresh = u.rows.filter((r) => !seen.has(key(r)) && r.ts >= Date.now() - RECENT_MS);
  u.rows.forEach((r) => seen.add(key(r)));
  if (!fresh.length) return;
  emit("call.update", { alarm: a, updates: u });
  if (isMine(a)) play("update");
  const open = () => { if (location.pathname === "/") emit("focus-alarm", a); else location.href = `/?alarm=${a.id}`; };
  toast(`<div class="call-toast-head">&#128222; <b>${esc(a.site_name)}</b> · ${esc(a.caption)}</div>
    ${fresh.map((r) => `<div>${esc(r.who)}: <b>${esc(r.what)}</b>${r.detail && r.what === "Guard note" ? ` <span class="muted">“${esc(r.detail)}”</span>` : ""}
      <span class="muted small-print">${esc(clock(r.ts))}</span></div>`).join("")}`,
  { kind: "call", timeout: 10000, onClick: open });
}

on("alarm.timeline", ({ alarm_id, guard }) => {
  if (!guard) return;
  clearTimeout(waiting.get(alarm_id));
  waiting.set(alarm_id, setTimeout(() => { waiting.delete(alarm_id); check(alarm_id); }, 400));
});

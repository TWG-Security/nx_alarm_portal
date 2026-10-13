// Top-bar clock: the server's NTP-synced time in this PC's time zone. The server's clock is fetched now and then
// (GET /api/time, half the round trip added); in between the PC ticks it forward. If the PC's own clock is off by
// more than WARN_MS it's flagged (the portal shows server time anyway), and if the server's clock lost NTP sync the
// clock turns red (app/services/clock.py).
import { setClockOffset, serverNow } from "./common.js";

const el = document.getElementById("sys-clock");
const SYNC_EVERY_MS = 5 * 60_000, WARN_MS = 2000;
let ntp = null, offset = 0, synced = false;
const fmt = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit", second: "2-digit" });      // zone: in the tooltip
const full = new Intl.DateTimeFormat([], { dateStyle: "full", timeStyle: "long" });

async function sync() {
  try {
    const t0 = Date.now();
    const r = await fetch("/api/time", { cache: "no-store", credentials: "same-origin" });
    const t1 = Date.now();
    if (!r.ok) return;
    const data = await r.json();
    offset = data.now_ms + (t1 - t0) / 2 - t1;          // server ahead of this PC by `offset` ms
    ntp = data.ntp; synced = true;
    setClockOffset(offset);
    paint();
  } catch { /* offline: keep the last offset */ }
}

function paint() {
  if (!el) return;
  const now = new Date(serverNow());
  el.textContent = fmt.format(now);
  const pcOff = Math.abs(offset) > WARN_MS, bad = ntp && ntp.ok === false;
  el.classList.toggle("sys-clock--warn", pcOff && !bad);
  el.classList.toggle("sys-clock--bad", !!bad);
  const lines = [full.format(now)];
  if (!synced) lines.push("Not checked against the server yet");
  else {
    if (ntp?.ok) lines.push(`Server clock synced (NTP ${ntp.server}, off by ${Math.abs(ntp.offset_ms)} ms)`);
    else if (bad) lines.push(`SERVER CLOCK OFF by ${Math.round(Math.abs(ntp.offset_ms) / 1000)} s (NTP ${ntp.server}): tell TWG support`);
    else lines.push("Server clock: NTP check pending");
    lines.push(pcOff ? `This PC's clock is ${Math.round(Math.abs(offset) / 1000)} s ${offset > 0 ? "slow" : "fast"}: the portal uses the server's time`
                     : "This PC's clock agrees with the server");
  }
  el.title = lines.join("\n");
}

if (el) {
  paint();
  sync();
  setInterval(paint, 1000);
  setInterval(sync, SYNC_EVERY_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") sync(); });
}

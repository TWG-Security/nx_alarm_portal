// Loaded on every signed-in page: live connection, alarm store, sound, critical pop-up, nav badge.
import { api, startLive, loadOpenAlarms, on, openCounts, toast, esc, isMine } from "./common.js";
import "./sound.js";
import "./clock.js";
import "./critical.js";
import "./drawer.js";
import "./call_updates.js";

const badge = document.getElementById("nav-alarm-count");
on("store", () => {
  if (!badge) return;
  const c = openCounts();
  const n = c[1] + c[2] + c[3];
  badge.hidden = n === 0;
  badge.textContent = n > 99 ? "99+" : String(n);
  badge.classList.toggle("pulse", c[1] + c[2] > 0);
  badge.classList.toggle("badge--warn", c[1] + c[2] === 0);
});

// Cameras badge: cameras with an image problem or warning that nobody muted (amber: never an alarm).
const healthBadge = document.getElementById("nav-health-count");
let healthTimer = null;
async function loadHealthBadge() {
  if (!healthBadge) return;
  const { total } = await api("/api/camera-health/summary").catch(() => ({ total: null }));
  if (!total) return;
  const n = (total.problem || 0) + (total.warning || 0);
  healthBadge.hidden = n === 0;
  healthBadge.textContent = n > 99 ? "99+" : String(n);
  healthBadge.title = `${total.problem || 0} camera problem(s), ${total.warning || 0} warning(s)`;
}
on("camera.health", () => { clearTimeout(healthTimer); healthTimer = setTimeout(loadHealthBadge, 1500); });
on("reconnect", loadHealthBadge);
loadHealthBadge();

// The map page has its own live feed; elsewhere, surface non-critical arrivals as toasts.
on("alarm.arrived", (a) => {
  if (!isMine(a) || a.priority === 1 || location.pathname === "/" || location.pathname.startsWith("/alarms")) return;
  toast(`<b>${esc(a.caption)}</b><br><span class="muted">${esc(a.site_name)}${a.source_name ? " · " + esc(a.source_name) : ""}</span>`,
    { kind: a.priority === 3 ? "warn" : "alarm", timeout: 8000, onClick: () => { location.href = `/?alarm=${a.id}`; } });
});

loadOpenAlarms().catch(() => {});
startLive();

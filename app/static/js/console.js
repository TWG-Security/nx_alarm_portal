// Loaded on every signed-in page: live connection, alarm store, sound, critical pop-up, nav badge.
import { startLive, loadOpenAlarms, on, openCounts, toast, esc } from "./common.js";
import "./sound.js";
import "./critical.js";
import "./drawer.js";

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

// The map page has its own live feed; elsewhere, surface non-critical arrivals as toasts.
on("alarm.arrived", (a) => {
  if (a.priority === 1 || location.pathname === "/" || location.pathname.startsWith("/alarms")) return;
  toast(`<b>${esc(a.caption)}</b><br><span class="muted">${esc(a.site_name)}${a.source_name ? " · " + esc(a.source_name) : ""}</span>`,
    { kind: a.priority === 3 ? "warn" : "alarm", timeout: 8000, onClick: () => { location.href = `/?alarm=${a.id}`; } });
});

loadOpenAlarms().catch(() => {});
startLive();

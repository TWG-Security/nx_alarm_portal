// The guard's location-sharing page (/g/<token>, app/routers/share.py). The sharing itself is share_core.js.
import { createSharer, isIOS, locationPermission } from "./share_core.js";
import { mountLocationTest } from "./location_test.js";

const { token, everyS } = window.PORTAL;
const $ = (id) => document.getElementById(id);
const KEY = "twg-share-on";                 // reopening the page resumes sharing

const sharer = createSharer(token, {
  everyS,
  onState(kind, text, facts) {
    $("share-state").className = `share-state share-state--${kind === "gone" || kind === "blocked" ? "off" : kind}`;
    if (kind === "blocked") document.querySelector(".share-help").open = true;
    $("share-state").textContent = text;
    $("share-facts").textContent = facts;
    const on = !["off", "gone", "blocked"].includes(kind);
    $("share-start").hidden = on; $("share-stop").hidden = !on;
    try { if (on) localStorage.setItem(KEY, "1"); else localStorage.removeItem(KEY); } catch { /* not remembered */ }
  },
});
$("share-start").addEventListener("click", () => sharer.start());          // one request, inside the tap
$("share-stop").addEventListener("click", () => sharer.stop());
if (isIOS()) document.querySelector(".ios-tip").hidden = false;
mountLocationTest(`/api/g/${encodeURIComponent(token)}/diag`);
// Reopened while sharing: carry on by itself only if the browser already allows location (no tap = no prompt).
(async () => {
  let again = false;
  try { again = localStorage.getItem(KEY) === "1"; } catch { /* not remembered */ }
  if (!again) return;
  if (await locationPermission() === "granted") sharer.start();
  else { $("share-state").textContent = "Tap Start sharing to share your location again"; $("share-state").className = "share-state share-state--wait"; }
})();
